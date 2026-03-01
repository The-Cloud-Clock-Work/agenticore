"""OpenBao (Vault-compatible) client for token storage."""

import time
from typing import Optional

import httpx

from auth_broker.config import get_config

_client: Optional[httpx.AsyncClient] = None


async def connect() -> None:
    global _client
    cfg = get_config()
    _client = httpx.AsyncClient(
        base_url=cfg.openbao_addr,
        headers={"X-Vault-Token": cfg.openbao_token},
        timeout=10.0,
    )


async def close() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def store_token(service: str, consumer_id: str, token_data: dict) -> None:
    """Store token at secret/data/auth-broker/{service}/{consumer_id} (KV v2)."""
    path = f"/v1/secret/data/auth-broker/{service}/{consumer_id}"
    payload = {"data": token_data}
    resp = await _client.post(path, json=payload)
    resp.raise_for_status()


async def get_token(service: str, consumer_id: str) -> Optional[dict]:
    """Retrieve token data. Returns None if not found."""
    path = f"/v1/secret/data/auth-broker/{service}/{consumer_id}"
    resp = await _client.get(path)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data", {}).get("data")


async def delete_token(service: str, consumer_id: str) -> None:
    """Delete all versions of a token secret."""
    path = f"/v1/secret/metadata/auth-broker/{service}/{consumer_id}"
    resp = await _client.delete(path)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()


def is_valid(token_data: dict) -> bool:
    """Manual tokens (expires_at=0) are always valid. OAuth: check with 30s buffer."""
    expires_at = int(token_data.get("expires_at", 0))
    if expires_at == 0:
        return bool(token_data.get("token"))
    return time.time() < expires_at - 30


def needs_refresh(token_data: dict) -> bool:
    """True if OAuth token has a refresh_token and expires within 5 minutes."""
    expires_at = int(token_data.get("expires_at", 0))
    if expires_at == 0 or not token_data.get("refresh_token"):
        return False
    return time.time() > expires_at - 300


async def refresh_token(service: str, consumer_id: str, provider: dict) -> Optional[dict]:
    """POST refresh_token grant → update stored token, return new data or None."""
    import os

    current = await get_token(service, consumer_id)
    if not current or not needs_refresh(current):
        return None

    client_id = os.getenv(provider.get("client_id_env", ""), "")
    client_secret = os.getenv(provider.get("client_secret_env", ""), "")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                provider["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current["refresh_token"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            token_resp = resp.json()
    except Exception:
        return None

    new_data = {
        "token": token_resp.get("access_token", ""),
        "refresh_token": token_resp.get("refresh_token", current.get("refresh_token", "")),
        "scope": token_resp.get("scope", current.get("scope", "")),
        "expires_at": int(time.time()) + token_resp.get("expires_in", 3600),
    }
    await store_token(service, consumer_id, new_data)
    return new_data
