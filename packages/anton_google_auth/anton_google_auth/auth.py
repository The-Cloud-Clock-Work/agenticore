"""Anton Google Auth — pluggable Google OAuth login for FastAPI services.

Drop-in Google OAuth + session management for any Anton FastAPI app.

Usage::

    from anton_google_auth import AntonGoogleAuth

    auth = AntonGoogleAuth(
        admin_email="you@gmail.com",
        redis_url="redis://localhost:6379/0",
        callback_base_url="https://myservice.homeofanton.com",
        title="MY SERVICE",
        subtitle="Control Panel",
    )

    app = FastAPI(lifespan=auth.lifespan)
    auth.install(app)

    # Protect any route with a one-liner dependency:
    @app.get("/secret")
    async def secret(email: str = Depends(auth.require_session)):
        return {"user": email}
"""

import os
import secrets as _secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._logo import ANTON_LOGO_B64
from .session import SessionStore

_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKENIZE = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

_LOGIN_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Sign In</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      height: 100%;
      background: #000;
      font-family: 'Courier New', monospace;
      color: #e0e0e0;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .container {{
      text-align: center;
      padding: 3rem 2rem;
      max-width: 400px;
      width: 100%;
    }}
    .logo {{
      width: 120px;
      height: 120px;
      margin: 0 auto 1.5rem;
      display: block;
      filter: drop-shadow(0 0 20px rgba(255,50,50,0.4));
    }}
    .title {{
      font-size: 1.8rem;
      font-weight: bold;
      color: #e0e0e0;
      letter-spacing: 0.15em;
      margin-bottom: 0.3rem;
    }}
    .subtitle {{
      font-size: 0.75rem;
      color: #444;
      letter-spacing: 0.3em;
      text-transform: uppercase;
      margin-bottom: 2.5rem;
    }}
    .google-btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.75rem;
      background: #111;
      border: 1px solid #333;
      color: #e0e0e0;
      padding: 0.75rem 1.5rem;
      border-radius: 4px;
      cursor: pointer;
      font-family: 'Courier New', monospace;
      font-size: 0.9rem;
      text-decoration: none;
      transition: border-color 0.2s, background 0.2s;
      letter-spacing: 0.05em;
    }}
    .google-btn:hover {{
      background: #1a1a1a;
      border-color: #555;
    }}
    .google-icon {{
      width: 18px;
      height: 18px;
      flex-shrink: 0;
    }}
    .error {{
      margin-top: 1.5rem;
      padding: 0.6rem 1rem;
      background: #2a0000;
      border: 1px solid #ff4444;
      color: #ff6666;
      border-radius: 3px;
      font-size: 0.8rem;
    }}
    .footer {{
      margin-top: 3rem;
      font-size: 0.65rem;
      color: #222;
      letter-spacing: 0.1em;
    }}
  </style>
</head>
<body>
  <div class="container">
    <img class="logo" src="data:image/png;base64,{{logo_b64}}" alt="{{title}}">
    <div class="title">{{title}}</div>
    <div class="subtitle">{{subtitle}}</div>
    <a href="/auth/google" class="google-btn">
      <svg class="google-icon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
        <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
        <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
        <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
      </svg>
      Sign in with Google
    </a>
    {{error_html}}
    <div class="footer">AUTHORIZED PERSONNEL ONLY</div>
  </div>
</body>
</html>
"""

_ERRORS = {
    "invalid_state": "Session expired — please try again.",
    "unauthorized": "Access denied — wrong Google account.",
    "exchange_failed": "Google login failed — please try again.",
}


class AntonGoogleAuth:
    """
    Pluggable Google OAuth + session module for Anton FastAPI services.

    Parameters
    ----------
    admin_email
        Single Google email allowed to log in. Empty = any Google account.
    redis_url
        Redis connection string. Falls back to AUTH_REDIS_URL or REDIS_URL env var.
    callback_base_url
        Public base URL of your service — ``/auth/google/callback`` appended automatically.
        Falls back to AUTH_CALLBACK_BASE_URL env var.
    google_client_id
        GCP OAuth client ID. Falls back to GOOGLE_CLIENT_ID env var.
    google_client_secret
        GCP OAuth client credential. Falls back to GOOGLE_CLIENT_SECRET env var.
    logo_b64
        Base64-encoded PNG to display on the login page. Defaults to Anton logo.
    title
        Service name on the login page. Default: ``ANTON``.
    subtitle
        Subtitle below the title. Default: ``Auth Broker``.
    """

    def __init__(
        self,
        admin_email: str = "",
        redis_url: str = "",
        callback_base_url: str = "",
        google_client_id: str = "",
        google_client_secret: str = "",  # nosecret
        logo_b64: str = "",
        title: str = "ANTON",
        subtitle: str = "Auth Broker",
    ) -> None:
        self.admin_email = admin_email or os.getenv("AUTH_BROKER_ADMIN_EMAIL", "")
        self._redis_url = redis_url or os.getenv("AUTH_REDIS_URL", os.getenv("REDIS_URL", ""))
        self._base = (callback_base_url or os.getenv("AUTH_CALLBACK_BASE_URL", "")).rstrip("/")
        self._cid = google_client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        self._cred = google_client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")  # nosecret
        self._logo = logo_b64 or ANTON_LOGO_B64
        self.title = title
        self.subtitle = subtitle
        self._store = SessionStore(self._redis_url)
        self._router = self._build_router()

    # ── Public API ────────────────────────────────────────────────────────────

    def install(self, app: FastAPI) -> None:
        """Mount /login, /auth/google, /auth/google/callback, /logout onto app."""
        app.include_router(self._router)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Use as ``FastAPI(lifespan=auth.lifespan)`` to manage Redis lifecycle."""
        await self._store.connect()
        yield
        await self._store.close()

    async def require_session(self, auth_session: str = Cookie(default="")) -> str:
        """FastAPI dependency — returns authenticated email or 307 → /login."""
        from fastapi import HTTPException
        email = await self._store.get(auth_session)
        if not email:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        return email

    async def optional_session(self, auth_session: str = Cookie(default="")) -> Optional[str]:
        """FastAPI dependency — returns email or None (no redirect)."""
        return await self._store.get(auth_session)

    async def check_request(self, request: Request,
                             auth_session: str = Cookie(default="")) -> Optional[str]:
        """
        Returns authenticated email or None.

        Checks in order:
        1. ``auth_session`` cookie
        2. ``Cf-Access-Authenticated-User-Email`` header (zero-trust compat)
        Auto-creates a session token from the CF Access header when trusted.
        """
        email = await self._store.get(auth_session)
        if email:
            return email
        cf_email = request.headers.get("Cf-Access-Authenticated-User-Email", "")
        if cf_email and (not self.admin_email or cf_email == self.admin_email):
            await self._store.create(cf_email)
            return cf_email
        return None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _cb_url(self) -> str:
        return f"{self._base}/auth/google/callback"

    def _render_login(self, error: str = "") -> str:
        err_html = f'<div class="error">{_ERRORS.get(error, "Login failed.")}</div>' if error else ""
        return _LOGIN_TEMPLATE.format(
            title=self.title, subtitle=self.subtitle,
            logo_b64=self._logo, error_html=err_html,
        )

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        _auth = self

        @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
        async def login_page(error: str = "") -> HTMLResponse:
            """Anton-branded Google login page — publicly accessible."""
            return HTMLResponse(content=_auth._render_login(error))

        @router.get("/auth/google", include_in_schema=False)
        async def google_start() -> RedirectResponse:
            """Begin Google OAuth flow."""
            state = _secrets.token_urlsafe(16)
            await _auth._store.save_state(state)
            params = {
                "client_id": _auth._cid,
                "redirect_uri": _auth._cb_url(),
                "scope": "openid email profile",
                "response_type": "code",
                "state": state,
                "prompt": "select_account",
            }
            return RedirectResponse(f"{_GOOGLE_AUTHORIZE}?{urlencode(params)}")

        @router.get("/auth/google/callback", include_in_schema=False)
        async def google_finish(code: str, state: str) -> RedirectResponse:
            """Exchange Google auth code, verify email, issue session cookie."""
            if not await _auth._store.consume_state(state):
                return RedirectResponse("/login?error=invalid_state")
            try:
                async with httpx.AsyncClient(timeout=15.0) as http:
                    tok = await http.post(_GOOGLE_TOKENIZE, data={
                        "code": code,
                        "client_id": _auth._cid,
                        "client_secret": _auth._cred,  # nosecret
                        "redirect_uri": _auth._cb_url(),
                        "grant_type": "authorization_code",
                    }, headers={"Accept": "application/json"})
                    tok.raise_for_status()
                    g_tok = tok.json().get("access_token", "")
                    me = await http.get(_GOOGLE_USERINFO,
                                        headers={"Authorization": "Bearer " + g_tok})
                    me.raise_for_status()
                    email = me.json().get("email", "")
            except Exception:
                return RedirectResponse("/login?error=exchange_failed")

            if _auth.admin_email and email != _auth.admin_email:
                return RedirectResponse("/login?error=unauthorized")

            token = await _auth._store.create(email)
            resp = RedirectResponse("/", status_code=302)
            resp.set_cookie("auth_session", token, httponly=True,
                            secure=True, samesite="lax", max_age=86400)
            return resp

        @router.get("/logout", include_in_schema=False)
        async def logout(auth_session: str = Cookie(default="")) -> RedirectResponse:
            await _auth._store.delete(auth_session)
            resp = RedirectResponse("/login", status_code=302)
            resp.delete_cookie("auth_session")
            return resp

        return router
