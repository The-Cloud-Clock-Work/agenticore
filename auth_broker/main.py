"""Auth Broker — human-in-the-loop OAuth token gateway.

Flow:
  1. K8s pod POST /auth/request {service}
  2. Broker stores request in Redis, builds OAuth URL, pushes WS notification
  3. Nestor clicks URL in dashboard → OAuth flow completes
  4. GET /auth/callback exchanges code, stores token in OpenBao
  5. Pod polls GET /auth/token/{id} until completed
"""

import logging
import os
import time
from contextlib import asynccontextmanager

_log = logging.getLogger(__name__)

from anton_google_auth import AntonGoogleAuth
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_broker import oauth, providers, store, vault
from auth_broker import ws as ws_module
from auth_broker.config import get_config
from auth_broker.models import AuthRequest, AuthStatus, ManualTokenInput

security = HTTPBearer()

# Google login module — env vars read at import time; Redis connects in lifespan
_google_auth = AntonGoogleAuth(
    admin_email=os.getenv("AUTH_BROKER_ADMIN_EMAIL", ""),
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/6"),
    callback_base_url=os.getenv("CALLBACK_BASE_URL", "http://localhost:8300"),
    google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),  # nosecret
    title="AUTH BROKER",
    subtitle="Token Gateway",
)


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
    await _google_auth._store.connect()
    yield
    await _google_auth._store.close()
    await store.close()
    await vault.close()


app = FastAPI(title="Auth Broker", version="0.1.0", lifespan=lifespan)
_google_auth.install(app)  # mounts /login, /auth/google, /auth/google/callback, /logout


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
        pkce_verifier = data.get("pkce_verifier", "")
        token_data = await oauth.exchange_code(provider, code, pkce_verifier, state)
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
        await store.append_event("token_ready", {"id": request_id, "service": data["service"]})
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
        await store.append_event("token_failed", {"id": request_id, "service": data["service"], "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e}")


# ── Dashboard ────────────────────────────────────────────────────────────────


def _serve_dashboard() -> HTMLResponse:
    """Serve dashboard HTML with API key injected as a JS global."""
    from pathlib import Path
    cfg = get_config()
    html = (Path(__file__).parent / "static" / "dashboard.html").read_text()
    injected = f'<script>window.__AUTH_KEY__="{cfg.api_key}";</script>'
    html = html.replace("</head>", injected + "</head>", 1)
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, auth_session: str = Cookie(default="")) -> HTMLResponse:
    """Root — check Google session or CF Access header, then serve dashboard."""
    if not await _google_auth.check_request(request, auth_session):
        return RedirectResponse("/login", status_code=302)
    return _serve_dashboard()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, token: str = "",
                    auth_session: str = Cookie(default="")) -> HTMLResponse:
    """Dashboard — session, legacy ?token=, or CF Access header."""
    cfg = get_config()
    if token:
        if token != cfg.api_key:
            raise HTTPException(status_code=401, detail="Invalid token")
        return _serve_dashboard()
    if not await _google_auth.check_request(request, auth_session):
        return RedirectResponse("/login", status_code=302)
    return _serve_dashboard()


# ── Auth request lifecycle ───────────────────────────────────────────────────


@app.post("/auth/request")
async def create_auth_request(
    req: AuthRequest, _: None = Depends(require_auth)
) -> dict:
    """Pod submits a token request for a service."""
    provider = providers.get_provider(req.service)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown service: {req.service}")

    # Pre-check: return cached token if one already exists and is valid
    existing = await vault.get_token(req.service, req.consumer_id)
    if existing:
        if vault.needs_refresh(existing):
            refreshed = await vault.refresh_token(req.service, req.consumer_id, provider)
            if refreshed:
                existing = refreshed
        if vault.is_valid(existing):
            return {"request_id": None, "status": AuthStatus.completed.value,
                    "token": existing, "cached": True}

    # Dedup: deny any existing pending requests for the same service+consumer_id
    existing_pending = await store.find_pending_by_service(req.service, req.consumer_id)
    for old_id in existing_pending:
        await store.update_status(old_id, AuthStatus.denied)
        await ws_module.ws_manager.broadcast("request_denied", {"id": old_id})
        await store.append_event("request_superseded", {"id": old_id, "service": req.service})

    request_id = await store.create_request(
        service=req.service,
        consumer_id=req.consumer_id,
        callback_url=req.callback_url,
        scopes=req.scopes or provider.get("scopes", []),
    )

    # Post-create dedup: deny any older pending for same service+consumer_id
    remaining = await store.find_pending_by_service(req.service, req.consumer_id)
    for old_id in remaining:
        if old_id != request_id:
            await store.update_status(old_id, AuthStatus.denied)
            await ws_module.ws_manager.broadcast("request_denied", {"id": old_id})
            await store.append_event("request_superseded", {"id": old_id, "replaced_by": request_id})

    auth_url = None
    if provider.get("auth_type") == "oauth2":
        request_data = await store.get_request(request_id)
        state = request_data["oauth_state"]
        code_challenge = ""
        if provider.get("pkce"):
            verifier, code_challenge = oauth.generate_pkce()
            await store.store_pkce_verifier(request_id, verifier)
        auth_url = await oauth.build_auth_url(provider, state, code_challenge)
        await store.store_oauth_state(state, request_id)
        manual_code = provider.get("manual_code", False)
        await store.update_status(
            request_id, AuthStatus.url_ready,
            auth_url=auth_url,
            manual_code="1" if manual_code else "0",
        )

    _event_data = {
        "id": request_id,
        "service": req.service,
        "consumer_id": req.consumer_id,
        "auth_type": provider.get("auth_type"),
        "auth_url": auth_url,
        "manual_code": provider.get("manual_code", False),
        "display_name": provider.get("display_name", req.service),
    }
    await ws_module.ws_manager.broadcast("new_request", _event_data)
    await store.append_event("new_request", _event_data)

    return {
        "request_id": request_id,
        "status": AuthStatus.url_ready.value if auth_url else AuthStatus.pending.value,
        "auth_url": auth_url,
    }


@app.get("/auth/token/direct")
async def get_token_direct(
    service: str, consumer_id: str = "default", _: None = Depends(require_auth)
) -> dict:
    """Direct token lookup — returns immediately without creating a request."""
    token_data = await vault.get_token(service, consumer_id)
    if not token_data:
        raise HTTPException(status_code=404, detail="No token found")
    if vault.needs_refresh(token_data):
        provider = providers.get_provider(service)
        if provider:
            refreshed = await vault.refresh_token(service, consumer_id, provider)
            if refreshed:
                token_data = refreshed
    return {
        "service": service,
        "consumer_id": consumer_id,
        "status": AuthStatus.completed.value,
        "token": token_data,
        "valid": vault.is_valid(token_data),
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
    await store.append_event("request_denied", {"id": request_id})
    return {"request_id": request_id, "status": "denied"}


@app.delete("/auth/requests/pending")
async def deny_all_pending(_: None = Depends(require_auth)) -> dict:
    """Deny all pending requests at once."""
    denied_ids = await store.deny_all_pending()
    for req_id in denied_ids:
        await ws_module.ws_manager.broadcast("request_denied", {"id": req_id})
        await store.append_event("request_denied", {"id": req_id})
    return {"denied": len(denied_ids), "ids": denied_ids}


@app.post("/auth/code/{request_id}")
async def submit_oauth_code(
    request_id: str, body: ManualTokenInput, _: None = Depends(require_auth)
) -> dict:
    """Exchange a manually-pasted OAuth code (for manual_code providers like Claude.ai)."""
    data = await store.get_request(request_id)
    if not data:
        raise HTTPException(status_code=404, detail="Request not found")
    provider = providers.get_provider(data["service"])
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    await store.update_status(request_id, AuthStatus.approved)
    try:
        # Normalize pasted value — handle three formats:
        #   1. plain code:  "gBJZMY..."
        #   2. code#state:  "gBJZMY...#a882c231-..."  (platform.claude.com format)
        #   3. full URL:    "https://platform.claude.com/oauth/code/callback?code=...&state=..."
        raw_code = body.token.strip()
        if raw_code.startswith("http"):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(raw_code)
            qs = parse_qs(parsed.query)
            raw_code = qs.get("code", [raw_code])[0]
        elif "#" in raw_code:
            raw_code = raw_code.split("#")[0]
        pkce_verifier = data.get("pkce_verifier", "")
        oauth_state = data.get("oauth_state", "")
        _log.warning("Exchanging code len=%d service=%s pkce=%s state=%s",
                     len(raw_code), data["service"], bool(pkce_verifier), bool(oauth_state))
        token_data = await oauth.exchange_code(provider, raw_code, pkce_verifier, oauth_state)
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
        await store.append_event("token_ready", {"id": request_id, "service": data["service"]})
        return {"request_id": request_id, "status": "completed"}
    except Exception as exc:
        _log.error("submit_oauth_code failed at %s: %r", type(exc).__name__, exc)
        await store.update_status(request_id, AuthStatus.pending)
        raise HTTPException(status_code=502, detail=f"Code exchange failed: {exc}") from exc


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
    await store.append_event("token_ready", {"id": request_id, "service": data["service"]})
    return {"request_id": request_id, "status": "completed"}


@app.get("/auth/events")
async def list_events(limit: int = 200, _: None = Depends(require_auth)) -> dict:
    """Persistent event log — newest first."""
    events = await store.get_events(limit=limit)
    return {"count": len(events), "events": events}


@app.get("/auth/history")
async def list_history(limit: int = 50, _: None = Depends(require_auth)) -> dict:
    """Completed/denied request audit trail — newest first."""
    history = await store.get_history(limit=limit)
    return {"count": len(history), "requests": history}


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
