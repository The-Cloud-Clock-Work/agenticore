"""OAuth2 authorization URL construction and code exchange — with PKCE support."""

import base64
import hashlib
import os
import secrets as _secrets
from urllib.parse import urlencode

import httpx

from auth_broker.config import get_config


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


async def exchange_code(provider: dict, code: str, code_verifier: str = "") -> dict:
    """Exchange authorization code for access token."""
    data: dict = {
        "client_id": _resolve_client_id(provider),
        "code": code,
        "redirect_uri": _callback_url(provider),
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    else:
        val = os.getenv(provider.get("client_secret_env", ""), "")
        if val:
            data["client_secret"] = val
    extra: dict = provider.get("token_headers", {})
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider["token_url"],
            data=data,
            headers={"Accept": "application/json", **extra},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()
