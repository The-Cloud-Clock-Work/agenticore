"""Auth Broker — human-in-the-loop OAuth token gateway.

Flow:
  1. K8s pod POST /auth/request {service}
  2. Broker stores request in Redis, builds OAuth URL, pushes WS notification
  3. Nestor clicks URL in dashboard → OAuth flow completes
  4. GET /auth/callback exchanges code, stores token in OpenBao
  5. Pod polls GET /auth/token/{id} until completed
"""

import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_broker import oauth, providers, store, vault
from auth_broker import ws as ws_module
from auth_broker.config import get_config
from auth_broker.models import AuthRequest, AuthStatus, ManualTokenInput

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

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
        pkce_verifier = data.get("pkce_verifier", "")
        token_data = await oauth.exchange_code(provider, code, pkce_verifier)
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


def _google_redirect_uri() -> str:
    cfg = get_config()
    return f"{cfg.callback_base_url.rstrip('/')}/auth/google/callback"


def _serve_dashboard() -> HTMLResponse:
    """Serve dashboard HTML with API key injected as a JS global."""
    from pathlib import Path
    cfg = get_config()
    html = (Path(__file__).parent / "static" / "dashboard.html").read_text()
    # Inject key so the dashboard JS never needs it in the URL
    injected = f'<script>window.__AUTH_KEY__="{cfg.api_key}";</script>'
    html = html.replace("</head>", injected + "</head>", 1)
    return HTMLResponse(content=html)


async def _session_from_cf_header(request: Request) -> str:
    """Auto-create a session from CF-Access-Authenticated-User-Email header."""
    cf_email = request.headers.get("Cf-Access-Authenticated-User-Email", "")
    if not cf_email:
        return ""
    cfg = get_config()
    if cfg.admin_email and cf_email != cfg.admin_email:
        return ""
    return await store.create_session(cf_email)


# ── Google OAuth login ────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = "") -> HTMLResponse:
    """Anton-branded Google OAuth entry page — no auth required."""
    from pathlib import Path
    html = (Path(__file__).parent / "static" / "login.html").read_text()
    if error:
        messages = {
            "invalid_state": "Session expired — please try again.",
            "unauthorized": "Access denied — wrong Google account.",
        }
        msg = messages.get(error, "Login failed.")
        html = html.replace("<!--ERROR-->", f'<div class="error">{msg}</div>')
    return HTMLResponse(content=html)


@app.get("/auth/google")
async def google_auth() -> RedirectResponse:
    """Redirect to Google OAuth consent screen."""
    import secrets as _sec
    state = _sec.token_urlsafe(16)
    await store.store_oauth_state(state, "__login__")
    params = {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": _google_redirect_uri(),
        "scope": "openid email profile",
        "response_type": "code",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@app.get("/auth/google/callback")
async def google_callback(code: str, state: str) -> RedirectResponse:
    """Exchange Google auth code, verify email, create session cookie."""
    stored = await store.get_request_by_state(state)
    if not stored:
        return RedirectResponse("/login?error=invalid_state")

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        }, headers={"Accept": "application/json"})
        token_resp.raise_for_status()
        g_token = token_resp.json().get("access_token", "")

        user_resp = await client.get(_GOOGLE_USERINFO_URL,
                                     headers={"Authorization": f"Bearer {g_token}"})
        user_resp.raise_for_status()
        email = user_resp.json().get("email", "")

    cfg = get_config()
    if cfg.admin_email and email != cfg.admin_email:
        return RedirectResponse("/login?error=unauthorized")

    session_token = await store.create_session(email)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("auth_session", session_token, httponly=True,
                        secure=True, samesite="lax", max_age=86400)
    return response


@app.get("/logout")
async def logout(auth_session: str = Cookie(default="")) -> RedirectResponse:
    await store.delete_session(auth_session)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("auth_session")
    return response


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, auth_session: str = Cookie(default="")) -> HTMLResponse:
    """Root — check session cookie or CF Access header, then serve dashboard."""
    if not await store.get_session(auth_session):
        new_token = await _session_from_cf_header(request)
        if not new_token:
            return RedirectResponse("/login", status_code=302)
        resp = _serve_dashboard()
        resp.set_cookie("auth_session", new_token, httponly=True,
                        secure=True, samesite="lax", max_age=86400)
        return resp
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
    if not await store.get_session(auth_session):
        new_token = await _session_from_cf_header(request)
        if not new_token:
            return RedirectResponse("/login", status_code=302)
        resp = _serve_dashboard()
        resp.set_cookie("auth_session", new_token, httponly=True,
                        secure=True, samesite="lax", max_age=86400)
        return resp
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

    await ws_module.ws_manager.broadcast(
        "new_request",
        {
            "id": request_id,
            "service": req.service,
            "consumer_id": req.consumer_id,
            "auth_type": provider.get("auth_type"),
            "auth_url": auth_url,
            "manual_code": provider.get("manual_code", False),
            "display_name": provider.get("display_name", req.service),
        },
    )

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
    return {"request_id": request_id, "status": "denied"}


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
        pkce_verifier = data.get("pkce_verifier", "")
        token_data = await oauth.exchange_code(provider, body.token, pkce_verifier)
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
        return {"request_id": request_id, "status": "completed"}
    except Exception as exc:
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
