"""OAuth2 authorization URL construction and code exchange."""

import os
from urllib.parse import urlencode

import httpx

from auth_broker.config import get_config


def _callback_url() -> str:
    cfg = get_config()
    return f"{cfg.callback_base_url.rstrip('/')}/auth/callback"


async def build_auth_url(provider: dict, state: str) -> str:
    """Construct the OAuth2 authorization URL for the given provider."""
    client_id = os.getenv(provider.get("client_id_env", ""), "")
    scopes = " ".join(provider.get("scopes", []))
    params = {
        "client_id": client_id,
        "redirect_uri": _callback_url(),
        "scope": scopes,
        "state": state,
        "response_type": "code",
    }
    return f"{provider['authorize_url']}?{urlencode(params)}"


async def exchange_code(provider: dict, code: str) -> dict:
    """Exchange authorization code for access token."""
    client_id = os.getenv(provider.get("client_id_env", ""), "")
    client_secret = os.getenv(provider.get("client_secret_env", ""), "")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": _callback_url(),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider["token_url"],
            data=data,
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()
