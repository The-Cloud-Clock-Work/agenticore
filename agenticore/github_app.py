"""GitHub App authentication — JWT generation and installation token exchange.

Produces short-lived installation access tokens (1 hour, refreshed at 55 min)
via the GitHub App REST API. Used as the highest-priority auth method when
``GITHUB_APP_ID``, ``GITHUB_APP_INSTALLATION_ID``, and a private key are configured.
"""

import logging
import time
from typing import Optional

import httpx
import jwt

from agenticore.config import get_config

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_JWT_EXPIRY = 600  # 10 minutes (GitHub maximum)
_TOKEN_REFRESH_BUFFER = 300  # refresh 5 minutes before expiry


class GitHubAppAuth:
    """Exchange GitHub App credentials for short-lived installation tokens."""

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str) -> None:
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._app_id and self._private_key_pem and self._installation_id)

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued 60s ago to account for clock drift
            "exp": now + _JWT_EXPIRY,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    def _exchange_for_installation_token(self) -> Optional[tuple[str, float]]:
        """POST /app/installations/{id}/access_tokens → (token, expires_at_epoch)."""
        app_jwt = self._generate_jwt()
        url = f"{_GITHUB_API}/app/installations/{self._installation_id}/access_tokens"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if resp.status_code != 201:
                logger.warning("GitHub App token exchange failed: HTTP %s — %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            token = data.get("token")
            # Parse ISO 8601 expires_at → epoch
            from datetime import datetime

            expires_str = data.get("expires_at", "")
            if expires_str:
                expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00")).timestamp()
            else:
                expires_at = time.time() + 3600  # default 1 hour
            return token, expires_at
        except Exception as exc:
            logger.warning("GitHub App token exchange error: %s", exc)
            return None

    def get_token(self) -> Optional[str]:
        """Return a valid installation token, refreshing if needed."""
        if not self.enabled:
            return None
        if self._cached_token and time.time() < (self._token_expires_at - _TOKEN_REFRESH_BUFFER):
            return self._cached_token
        result = self._exchange_for_installation_token()
        if result is None:
            return None
        self._cached_token, self._token_expires_at = result
        logger.debug("GitHub App installation token refreshed (expires %.0f)", self._token_expires_at)
        return self._cached_token


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[GitHubAppAuth] = None


def get_github_app_auth() -> GitHubAppAuth:
    """Lazy-init singleton from config."""
    global _instance
    if _instance is None:
        cfg = get_config()
        _instance = GitHubAppAuth(
            app_id=cfg.github.app_id,
            private_key_pem=cfg.github.app_private_key,
            installation_id=cfg.github.app_installation_id,
        )
    return _instance


def reset_github_app_auth() -> None:
    """Reset singleton (for testing)."""
    global _instance
    _instance = None
