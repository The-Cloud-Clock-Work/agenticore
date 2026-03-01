"""OpenBao (Vault-compatible) client for token storage."""

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
