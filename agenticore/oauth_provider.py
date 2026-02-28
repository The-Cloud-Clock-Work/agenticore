"""OAuth 2.1 Authorization Server Provider for Agenticore.

Implements the MCP OAuthAuthorizationServerProvider protocol with in-memory
storage. Enables claude.ai and other MCP clients to connect via OAuth 2.1
(authorization_code + PKCE) while API key auth continues working for CLI
and REST clients.

Activated when OAUTH_ISSUER_URL is set.
"""

import os
import secrets
import time
from typing import Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# Token TTLs
_AUTH_CODE_TTL = 300  # 5 minutes
_ACCESS_TOKEN_TTL = 3600  # 1 hour
_REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days


def _get_redirect_uris() -> list:
    """Load allowed redirect URIs from environment."""
    from pydantic import AnyUrl

    raw = os.getenv("OAUTH_ALLOWED_REDIRECT_URIS", "")
    if not raw.strip():
        return []
    return [AnyUrl(u.strip()) for u in raw.split(",") if u.strip()]


class AgenticoreOAuthProvider(OAuthAuthorizationServerProvider):
    """In-memory OAuth 2.1 provider for Agenticore.

    Auto-approves authorization requests (no consent page — private server).

    When OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET are pre-configured, dynamic
    registration returns those credentials. claude.ai requires dynamic
    registration to work; the provider returns pre-configured creds when
    locked so the full flow completes.

    API keys (AGENTICORE_API_KEYS) are accepted as Bearer tokens via the
    load_access_token fallback, so existing clients keep working.
    """

    def __init__(self, issuer_url: str, client_id: str = "", client_secret: str = ""):
        self.issuer_url = issuer_url
        self._locked = bool(client_id and client_secret)

        # In-memory stores
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._token_pairs: dict[str, str] = {}  # access_token -> refresh_token

        if self._locked:
            redirect_uris = _get_redirect_uris()
            if not redirect_uris:
                from pydantic import AnyUrl

                redirect_uris = [AnyUrl(issuer_url.rstrip("/") + "/callback")]

            allowed_scopes = os.getenv("OAUTH_ALLOWED_SCOPES", "").strip() or None

            self._clients[client_id] = OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                client_id_issued_at=int(time.time()),
                redirect_uris=redirect_uris,
                scope=allowed_scopes,
                token_endpoint_auth_method="client_secret_post",
            )

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if self._locked:
            # Return pre-configured credentials — claude.ai requires dynamic
            # registration to succeed, so we return our creds instead of rejecting.
            pre = next(iter(self._clients.values()))
            client_info.client_id = pre.client_id
            client_info.client_secret = pre.client_secret
            client_info.client_id_issued_at = pre.client_id_issued_at

            # Merge redirect URIs
            existing = {str(u) for u in (pre.redirect_uris or [])}
            merged = list(pre.redirect_uris or [])
            for uri in client_info.redirect_uris or []:
                if str(uri) not in existing:
                    merged.append(uri)
            client_info.redirect_uris = merged
            pre.redirect_uris = merged
            self._clients[pre.client_id] = pre
            return

        client_id = secrets.token_urlsafe(16)
        client_info.client_id = client_id
        client_info.client_id_issued_at = int(time.time())
        self._clients[client_id] = client_info

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not client.client_id:
            raise AuthorizeError(error="invalid_request", error_description="Client has no client_id")

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + _AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )

        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AuthorizationCode]:
        auth_code = self._auth_codes.get(authorization_code)
        if auth_code is None:
            return None
        if auth_code.client_id != client.client_id:
            return None
        if time.time() > auth_code.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        return auth_code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        now = int(time.time())
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)

        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + _ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=authorization_code.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + _REFRESH_TOKEN_TTL,
        )
        self._token_pairs[access_token] = refresh_token

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Optional[RefreshToken]:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None:
            return None
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at is not None and time.time() > rt.expires_at:
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)

        now = int(time.time())
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        effective_scopes = scopes if scopes else refresh_token.scopes

        self._access_tokens[new_access] = AccessToken(
            token=new_access,
            client_id=refresh_token.client_id,
            scopes=effective_scopes,
            expires_at=now + _ACCESS_TOKEN_TTL,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=refresh_token.client_id,
            scopes=effective_scopes,
            expires_at=now + _REFRESH_TOKEN_TTL,
        )
        self._token_pairs[new_access] = new_refresh

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL,
            scope=" ".join(effective_scopes) if effective_scopes else None,
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        # Check OAuth tokens first
        at = self._access_tokens.get(token)
        if at is not None:
            if at.expires_at is not None and time.time() > at.expires_at:
                self._access_tokens.pop(token, None)
                return None
            return at

        # Fallback: accept API keys as pre-issued tokens (no expiry)
        raw = os.getenv("AGENTICORE_API_KEYS", "")
        if raw.strip():
            api_keys = [k.strip() for k in raw.split(",") if k.strip()]
            if token in api_keys:
                return AccessToken(token=token, client_id="api-key-client", scopes=[])

        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
            paired_rt = self._token_pairs.pop(token.token, None)
            if paired_rt:
                self._refresh_tokens.pop(paired_rt, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
            for at_str, rt_str in list(self._token_pairs.items()):
                if rt_str == token.token:
                    self._access_tokens.pop(at_str, None)
                    self._token_pairs.pop(at_str, None)
                    break
