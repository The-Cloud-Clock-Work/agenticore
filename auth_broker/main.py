"""Auth Broker — human-in-the-loop OAuth token gateway.

Flow:
  1. K8s pod POST /auth/request {service}
  2. Broker stores request in Redis, builds OAuth URL, pushes WS notification
  3. Nestor clicks URL in dashboard → OAuth flow completes
  4. GET /auth/callback exchanges code, stores token in OpenBao
  5. Pod polls GET /auth/token/{id} until completed
"""

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_broker import oauth, providers, store, vault
from auth_broker import ws as ws_module
from auth_broker.config import get_config
from auth_broker.models import AuthRequest, AuthStatus, ManualTokenInput

security = HTTPBearer()


def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    cfg = get_config()
    if credentials.credentials != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    providers.load_providers()
    await store.connect(cfg.redis_url)
    await vault.connect()
    yield
    await store.close()
    await vault.close()


app = FastAPI(title="Auth Broker", version="0.1.0", lifespan=lifespan)


# ── Public ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "auth-broker"}


@app.get("/auth/callback")
async def oauth_callback(code: str, state: str) -> HTMLResponse:
    """OAuth redirect receiver — exchanges code and stores token in OpenBao."""
    request_id = await store.get_request_by_state(state)
    if not request_id:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")

    provider = providers.get_provider(data["service"])
    await store.update_status(request_id, AuthStatus.approved)

    try:
        token_data = await oauth.exchange_code(provider, code)
        vault_data = {
            "token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "scope": token_data.get("scope", ""),
            "expires_at": int(time.time()) + token_data.get("expires_in", 3600),
        }
        await vault.store_token(data["service"], data["consumer_id"], vault_data)
        await store.update_status(request_id, AuthStatus.completed)
        await ws_module.ws_manager.broadcast(
            "token_ready", {"id": request_id, "service": data["service"]}
        )
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:2rem'>"
            "<h2>✅ Authorized</h2>"
            "<p>Token stored in OpenBao. You can close this tab.</p>"
            "</body></html>"
        )
    except Exception as e:
        await store.update_status(request_id, AuthStatus.denied, error=str(e))
        await ws_module.ws_manager.broadcast(
            "token_failed", {"id": request_id, "service": data["service"], "error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e}")


# ── Dashboard ────────────────────────────────────────────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_auth)) -> HTMLResponse:
    from pathlib import Path

    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text())


# ── Auth request lifecycle ───────────────────────────────────────────────────


@app.post("/auth/request")
async def create_auth_request(
    req: AuthRequest, _: None = Depends(require_auth)
) -> dict:
    """Pod submits a token request for a service."""
    provider = providers.get_provider(req.service)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown service: {req.service}")

    request_id = await store.create_request(
        service=req.service,
        consumer_id=req.consumer_id,
        callback_url=req.callback_url,
        scopes=req.scopes or provider.get("scopes", []),
    )

    auth_url = None
    if provider.get("auth_type") == "oauth2":
        request_data = await store.get_request(request_id)
        state = request_data["oauth_state"]
        auth_url = await oauth.build_auth_url(provider, state)
        await store.store_oauth_state(state, request_id)
        await store.update_status(request_id, AuthStatus.url_ready, auth_url=auth_url)

    await ws_module.ws_manager.broadcast(
        "new_request",
        {
            "id": request_id,
            "service": req.service,
            "consumer_id": req.consumer_id,
            "auth_type": provider.get("auth_type"),
            "auth_url": auth_url,
            "display_name": provider.get("display_name", req.service),
        },
    )

    return {
        "request_id": request_id,
        "status": AuthStatus.url_ready.value if auth_url else AuthStatus.pending.value,
        "auth_url": auth_url,
    }


@app.get("/auth/pending")
async def list_pending(_: None = Depends(require_auth)) -> dict:
    pending = await store.get_pending()
    return {"count": len(pending), "requests": pending}


@app.get("/auth/url/{request_id}")
async def get_auth_url(request_id: str, _: None = Depends(require_auth)) -> dict:
    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")
    return {
        "request_id": request_id,
        "auth_url": data.get("auth_url", ""),
        "status": data["status"],
    }


@app.get("/auth/token/{request_id}")
async def get_token(request_id: str, _: None = Depends(require_auth)) -> dict:
    """Poll for a completed token. Returns token=null while pending."""
    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")

    if data["status"] != AuthStatus.completed.value:
        return {"request_id": request_id, "status": data["status"], "token": None}

    token_data = await vault.get_token(data["service"], data["consumer_id"])
    return {"request_id": request_id, "status": data["status"], "token": token_data}


@app.delete("/auth/request/{request_id}")
async def deny_request(request_id: str, _: None = Depends(require_auth)) -> dict:
    """Cancel or deny a pending request."""
    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")
    await store.update_status(request_id, AuthStatus.denied)
    await ws_module.ws_manager.broadcast("request_denied", {"id": request_id})
    return {"request_id": request_id, "status": "denied"}


@app.post("/auth/manual/{request_id}")
async def manual_token(
    request_id: str, body: ManualTokenInput, _: None = Depends(require_auth)
) -> dict:
    """Submit a token manually (for non-OAuth providers like Anthropic)."""
    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")

    vault_data = {
        "token": body.token,
        "refresh_token": "",
        "scope": body.scope,
        "expires_at": body.expires_at,
    }
    await vault.store_token(data["service"], data["consumer_id"], vault_data)
    await store.update_status(request_id, AuthStatus.completed)
    await ws_module.ws_manager.broadcast(
        "token_ready", {"id": request_id, "service": data["service"]}
    )
    return {"request_id": request_id, "status": "completed"}


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/auth/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Dashboard WebSocket — auth via ?token= query param."""
    cfg = get_config()
    token = ws.query_params.get("token", "")
    if token != cfg.api_key:
        await ws.close(code=4001)
        return

    await ws_module.ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive ping/pong
    except WebSocketDisconnect:
        await ws_module.ws_manager.disconnect(ws)
