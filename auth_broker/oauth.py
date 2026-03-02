"""OAuth2 authorization URL construction and code exchange — with PKCE support."""

import base64
import hashlib
import logging
import os
import secrets as _secrets
from urllib.parse import urlencode

import httpx

from auth_broker.config import get_config

_log = logging.getLogger(__name__)


def _callback_url(provider: dict | None = None) -> str:
    if provider and provider.get("redirect_uri_override"):
        return provider["redirect_uri_override"]
    cfg = get_config()
    return f"{cfg.callback_base_url.rstrip('/')}/auth/callback"


def _resolve_client_id(provider: dict) -> str:
    """Direct value takes priority over env var lookup."""
    return provider.get("client_id") or os.getenv(provider.get("client_id_env", ""), "")


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) per RFC 7636 S256."""
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def build_auth_url(provider: dict, state: str, code_challenge: str = "") -> str:
    """Construct the OAuth2 authorization URL for the given provider."""
    params = {
        "client_id": _resolve_client_id(provider),
        "redirect_uri": _callback_url(provider),
        "scope": " ".join(provider.get("scopes", [])),
        "state": state,
        "response_type": "code",
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{provider['authorize_url']}?{urlencode(params)}"


async def exchange_code(provider: dict, code: str, code_verifier: str = "", state: str = "") -> dict:
    """Exchange authorization code for access token."""
    data: dict = {
        "client_id": _resolve_client_id(provider),
        "code": code,
        "redirect_uri": _callback_url(provider),
        "grant_type": "authorization_code",
    }
    if state:
        data["state"] = state
    if code_verifier:
        data["code_verifier"] = code_verifier
    else:
        val = os.getenv(provider.get("client_secret_env", ""), "")
        if val:
            data["client_secret"] = val
    # token_headers used for auth headers (e.g. Accept), NOT for body format hints
    # strip any keys that should not go on the token request
    extra: dict = {k: v for k, v in provider.get("token_headers", {}).items()
                   if k.lower() not in ("anthropic-beta",)}
    async with httpx.AsyncClient() as client:
        use_json = provider.get("token_format") == "json"
        resp = await client.post(
            provider["token_url"],
            json=data if use_json else None,
            data=None if use_json else data,
            headers={"Accept": "application/json", **extra},
            timeout=15.0,
        )
        if not resp.is_success:
            _log.error("Token exchange failed: %s %s — %s", resp.status_code, provider["token_url"], resp.text)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {resp.text}",
                request=resp.request,
                response=resp,
            )
        return resp.json()
