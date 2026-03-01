"""Auth Broker client — synchronous token retrieval for subprocess environments."""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class AuthClient:
    """Synchronous client for the Auth Broker service.

    Uses sync httpx so it can be called from _build_env() without async machinery.
    Connection errors are caught and logged — callers always get None on failure.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        poll_interval: int = 5,
    ) -> None:
        self._url = url or os.getenv("AUTH_BROKER_URL", "")
        self._api_key = api_key or os.getenv("AUTH_BROKER_API_KEY", "")
        self._poll_interval = poll_interval

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def get_token(self, service: str, consumer_id: str = "default", timeout: int = 300) -> Optional[dict]:
        """Get a valid token for the given service.

        Fast path: GET /auth/token/direct — returns immediately if token exists.
        Slow path: POST /auth/request → poll GET /auth/token/{id} until completed/denied/timeout.

        Returns None on auth broker disabled, connection error, denial, or timeout.
        """
        if not self.enabled:
            return None

        base = self._url.rstrip("/")

        # Fast path: check for existing valid token
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    f"{base}/auth/token/direct",
                    params={"service": service, "consumer_id": consumer_id},
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token")
        except httpx.ConnectError as e:
            logger.warning("Auth Broker unreachable: %s", e)
            return None
        except Exception as e:
            logger.warning("Auth Broker fast-path error: %s", e)

        # Slow path: create request and poll
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{base}/auth/request",
                    json={"service": service, "consumer_id": consumer_id},
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                logger.warning("Auth Broker request failed: HTTP %s", resp.status_code)
                return None
            result = resp.json()
        except httpx.ConnectError as e:
            logger.warning("Auth Broker unreachable: %s", e)
            return None
        except Exception as e:
            logger.warning("Auth Broker slow-path request error: %s", e)
            return None

        # Immediate return if cached
        if result.get("cached") or result.get("status") == "completed":
            return result.get("token")

        request_id = result.get("request_id")
        if not request_id:
            return None

        # Poll until completed, denied, or timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            try:
                with httpx.Client(timeout=10.0) as client:
                    poll = client.get(
                        f"{base}/auth/token/{request_id}",
                        headers=self._headers(),
                    )
                if poll.status_code != 200:
                    continue
                poll_data = poll.json()
                status = poll_data.get("status")
                if status == "completed":
                    return poll_data.get("token")
                if status == "denied":
                    logger.warning("Auth Broker denied token for service=%s", service)
                    return None
            except Exception as e:
                logger.warning("Auth Broker poll error: %s", e)

        logger.warning("Auth Broker timeout waiting for service=%s (request_id=%s)", service, request_id)
        return None

    def get_credential(self, service: str, consumer_id: str = "default") -> Optional[str]:
        """Returns the token string or None."""
        data = self.get_token(service, consumer_id=consumer_id)
        if not data:
            return None
        return data.get("token") if isinstance(data, dict) else None
