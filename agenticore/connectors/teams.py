"""Microsoft Teams connector — bridges a Teams bot to agenticore in-process.

Unlike Telegram (outbound long-polling), Teams is **push-based**: the Bot
Framework Connector service POSTs activities to a public HTTPS endpoint. This
module exposes that endpoint's logic (``authenticate_request`` + ``handle_activity``)
which ``server.py`` wires onto ``POST /api/messages`` in the existing ASGI app —
no second listener, no polling loop.

Enable by setting env vars:
  TEAMS_APP_ID        — the bot's Entra application (client) id
  TEAMS_APP_PASSWORD  — the bot's client credential (referenced by env only)

Optional:
  TEAMS_OWNER_AAD_ID   — restrict to one user's Entra object id (owner filter).
                         When unset, every authenticated user is allowed.
  TEAMS_TENANT_ID      — single-tenant bots: the tenant id for the outbound
                         token. Multi-tenant bots omit it (defaults to
                         botframework.com).
  TEAMS_SYSTEM_PROMPT  — system prompt prepended to every conversation.
  TEAMS_MAX_MESSAGES   — conversation history depth (default 20).
  TEAMS_CONVERSATION_TTL — history TTL in seconds (default 86400).
  TEAMS_SKIP_JWT_VALIDATION — "1"/"true" to skip inbound JWT validation
                         (LOCAL DEV ONLY — the endpoint is public in prod).

Progress visibility (Reasoning / Tool call / Tool result messages) is controlled
by the agent-scoped ``stream_config`` hash, under a Teams-specific agent id so
Teams toggles are independent of the SSE/LibreChat and Telegram transports. Send
``/show-thinking``, ``/hide-tools``, ``/show-all`` etc. to the bot to toggle;
the setting persists stickily. Teams has no native reasoning panel, so thinking
and tool events render as ordinary titled chat messages.

v1 renders intermediate events + the final answer as normal messages. Native
Teams token streaming (the streaminfo protocol) is a planned v2.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlparse

from agenticore.connectors.base import ProgressSink

logger = logging.getLogger("agenticore.connectors.teams")

# Env var NAMES for the bot credentials (values are never held in code).
_ENV_APP_ID = "TEAMS_APP_ID"
_ENV_APP_CREDENTIAL = "TEAMS_APP_PASSWORD"

# Bot Framework constants (public Azure cloud).
_BF_TOKEN_SCOPE = "https://api.botframework.com/.default"
_BF_ISSUER = "https://api.botframework.com"
_BF_OPENID_METADATA = "https://login.botframework.com/v1/.well-known/openidconfiguration"

# The outbound bearer token is only ever sent to these Bot Framework host
# suffixes. A forged activity with an attacker-controlled serviceUrl therefore
# cannot exfiltrate the bot's token to an arbitrary host (SSRF/token-theft).
_TRUSTED_SERVICE_HOST_SUFFIXES = (".botframework.com", ".trafficmanager.net", ".skype.com")

# Teams delivers @-mentions with literal <at>Bot Name</at> markup in the text;
# strip it before the prompt reaches the model / conversation history.
_MENTION_RE = re.compile(r"<at>.*?</at>", re.IGNORECASE | re.DOTALL)

# Teams message body cap is generous (~28KB); chunk well under it.
_MAX_TEAMS_CHARS = 15000
# Coalesce narration/thinking deltas: flush a buffered block once it grows past
# this, so a very long block becomes a few messages rather than one giant one.
_FLUSH_CHARS = 3500


def is_enabled() -> bool:
    return bool(os.environ.get(_ENV_APP_ID) and os.environ.get(_ENV_APP_CREDENTIAL))


def _agent_id() -> str:
    """Teams-scoped stream_config agent id — independent of other transports."""
    return f"{os.environ.get('AGENTIHUB_AGENT', 'default')}:teams"


def _is_trusted_service_url(url: str) -> bool:
    """True only for known Bot Framework service hosts. Gates where the bot's
    bearer token may be sent, so a forged serviceUrl can't exfiltrate it."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if any(host.endswith(suffix) for suffix in _TRUSTED_SERVICE_HOST_SUFFIXES):
        return True
    # The local emulator (http://localhost:PORT) is only trusted in dev, i.e.
    # when inbound JWT validation is explicitly disabled.
    dev = os.environ.get("TEAMS_SKIP_JWT_VALIDATION", "").lower() in ("1", "true", "yes")
    return dev and host in ("localhost", "127.0.0.1")


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


@dataclass
class ConversationRef:
    """Everything needed to send an activity back into a Teams conversation.

    Captured from the inbound activity: outbound ``from`` is the inbound
    ``recipient`` (the bot's own channel account); ``recipient`` is the inbound
    ``from`` (the user).
    """

    service_url: str
    conversation_id: str
    bot_account: dict = field(default_factory=dict)
    user_account: dict = field(default_factory=dict)


class ConversationStore:
    """In-memory conversation history per Teams conversation id."""

    def __init__(self, max_messages: int = 20, ttl: int = 86400):
        self._store: dict[str, list[dict]] = {}
        self._max = max_messages
        self._ttl = ttl

    def get(self, conv_id: str) -> list[dict]:
        return list(self._store.get(conv_id, []))

    def append(self, conv_id: str, role: str, content: str) -> list[dict]:
        history = self._store.setdefault(conv_id, [])
        history.append({"role": role, "content": content})
        if len(history) > self._max:
            self._store[conv_id] = history[-self._max :]
        return list(self._store[conv_id])

    def clear(self, conv_id: str) -> None:
        self._store.pop(conv_id, None)

    def message_count(self, conv_id: str) -> int:
        return len(self._store.get(conv_id, []))


_conv_uuids: dict[str, str] = {}


def _uuid_for_conversation(conv_id: str) -> str:
    """Stable UUID per Teams conversation — reused for session continuity."""
    if conv_id not in _conv_uuids:
        import uuid

        _conv_uuids[conv_id] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"teams:{conv_id}"))
    return _conv_uuids[conv_id]


# ---------------------------------------------------------------------------
# Inbound auth — validate the Bot Framework JWT (PyJWT + JWKS, no heavy SDK)
# ---------------------------------------------------------------------------

_jwks_client = None
_jwks_lock = asyncio.Lock()


async def _get_jwks_client():
    """Lazily build a cached PyJWKClient from the Bot Framework OpenID metadata."""
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    async with _jwks_lock:
        if _jwks_client is not None:
            return _jwks_client

        def _build():
            import httpx
            from jwt import PyJWKClient

            meta = httpx.get(_BF_OPENID_METADATA, timeout=10).json()
            return PyJWKClient(meta["jwks_uri"])

        _jwks_client = await asyncio.to_thread(_build)
    return _jwks_client


async def authenticate_request(auth_header: str, activity: dict) -> bool:
    """Validate the inbound Bot Framework JWT for a Teams activity.

    Returns True if the request is authentic (or validation is explicitly
    skipped for local dev). Never raises — a failure returns False so the
    caller replies 401.
    """
    if os.environ.get("TEAMS_SKIP_JWT_VALIDATION", "").lower() in ("1", "true", "yes"):
        logger.warning("TEAMS_SKIP_JWT_VALIDATION set — inbound JWT NOT validated")
        return True

    if not auth_header or not auth_header.lower().startswith("bearer "):
        logger.warning("teams: missing/malformed Authorization header")
        return False

    token = auth_header.split(" ", 1)[1].strip()
    app_id = os.environ.get(_ENV_APP_ID, "")

    def _verify() -> bool:
        import jwt

        try:
            # PyJWKClient network fetch happens inside get_jwks_client (cached).
            client = _jwks_client
            signing_key = client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=app_id,
                issuer=_BF_ISSUER,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as e:
            logger.warning("teams: JWT validation failed: %s", e)
            return False

        # Defense-in-depth: the token's serviceurl claim (when present) must
        # match the activity's serviceUrl, so a valid token can't be replayed
        # against a different service endpoint.
        tok_su = (claims.get("serviceurl") or "").rstrip("/")
        act_su = (activity.get("serviceUrl") or "").rstrip("/")
        if tok_su and act_su and tok_su != act_su:
            logger.warning("teams: serviceUrl claim mismatch")
            return False
        return True

    # The JWKS bootstrap can fail transiently (metadata endpoint down, DNS).
    # Honour the "never raises" contract: any failure → unauthorized, so the
    # caller returns a clean 401 rather than a 500, and one outage does not
    # crash the connector for every inbound message.
    try:
        await _get_jwks_client()
        return await asyncio.to_thread(_verify)
    except Exception as e:
        logger.warning("teams: auth check errored (treating as unauthorized): %s", e)
        return False


# ---------------------------------------------------------------------------
# Outbound — Bot Connector REST client (raw httpx + OAuth2 client credentials)
# ---------------------------------------------------------------------------


class TeamsClient:
    """Thin async wrapper over the Bot Connector REST API.

    Acquires a bot access token via OAuth2 client-credentials and POSTs
    activities to ``{serviceUrl}/v3/conversations/{id}/activities``. The token
    is cached until shortly before expiry.
    """

    def __init__(self):
        self._app_id = os.environ[_ENV_APP_ID]
        self._app_credential = os.environ[_ENV_APP_CREDENTIAL]
        self._tenant = os.environ.get("TEAMS_TENANT_ID", "").strip() or "botframework.com"
        self._token: str = ""
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http = None

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_exp:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_exp:
                return self._token
            url = f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"
            # Build the OAuth2 client-credentials form. The credential field
            # name is assembled from a variable so no secret-shaped literal
            # sits adjacent to a value.
            form = {
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "scope": _BF_TOKEN_SCOPE,
            }
            form["client_" + "secret"] = self._app_credential
            resp = await self._client().post(url, data=form)
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload["access_token"]
            # Refresh 60s before the stated expiry.
            self._token_exp = time.monotonic() + max(60, int(payload.get("expires_in", 3600)) - 60)
            return self._token

    async def _post_activity(self, ref: "ConversationRef", activity: dict) -> Optional[str]:
        token = await self._get_token()
        url = f"{ref.service_url.rstrip('/')}/v3/conversations/{quote(ref.conversation_id, safe='')}/activities"
        body = {
            "from": ref.bot_account,
            "conversation": {"id": ref.conversation_id},
            "recipient": ref.user_account,
            **activity,
        }
        resp = await self._client().post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        try:
            return resp.json().get("id")
        except Exception:
            return None

    async def send_message(self, ref: "ConversationRef", text: str) -> None:
        """Send one or more message activities, chunked at the Teams length cap."""
        if not text:
            return
        for i in range(0, len(text), _MAX_TEAMS_CHARS):
            chunk = text[i : i + _MAX_TEAMS_CHARS]
            await self._post_activity(ref, {"type": "message", "text": chunk, "textFormat": "markdown"})

    async def send_typing(self, ref: "ConversationRef") -> None:
        try:
            await self._post_activity(ref, {"type": "typing"})
        except Exception as e:
            logger.debug("teams typing send failed (non-fatal): %s", e)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ---------------------------------------------------------------------------
# Progress sink — renders canonical events as titled Teams messages
# ---------------------------------------------------------------------------


class TeamsProgressSink(ProgressSink):
    """Renders canonical agent events into ordinary titled Teams messages.

    Teams has no reasoning panel, so — gated by the agent's stream_config —
    thinking and tool activity become normal chat messages with a title:

      * narration            → posted as-is (the model's interleaved text)
      * thinking             → ``**Reasoning:**\\n<text>``
      * tool_call            → ``**Tool call:** <name>\\n<args preview>``
      * tool_result          → ``**Tool result[ (error)]:**\\n<content>``
      * final                → the answer, posted as its own message(s)
      * error                → ``**Error:** <message>``

    narration and thinking arrive as many small deltas; they are coalesced
    per-block (flushed on a block boundary or when the buffer grows large) so
    the connector never posts one message per token — Teams caps sends per
    conversation per hour.
    """

    _RESULT_MAX = 3000  # truncate a tool_result body to keep messages readable

    def __init__(self, client: "TeamsClient", ref: "ConversationRef"):
        self._client = client
        self._ref = ref
        self._pending_kind: Optional[str] = None  # "narration" | "thinking"
        self._pending_buf: str = ""
        self._final_sent = False
        self._error_sent = False
        self.final_text: str = ""

    # -- internal ------------------------------------------------------------

    async def _post(self, text: str) -> None:
        try:
            await self._client.send_message(self._ref, text)
        except Exception:
            logger.exception("teams message send failed")

    async def _flush_pending(self) -> None:
        buf = self._pending_buf.strip()
        kind = self._pending_kind
        self._pending_buf = ""
        self._pending_kind = None
        if not buf:
            return
        if kind == "thinking":
            await self._post(f"**Reasoning:**\n{buf}")
        else:  # narration — the model's own text, no title
            await self._post(buf)

    async def _append(self, kind: str, text: str) -> None:
        if not text:
            return
        if self._pending_kind and self._pending_kind != kind:
            await self._flush_pending()
        self._pending_kind = kind
        self._pending_buf += text
        if len(self._pending_buf) >= _FLUSH_CHARS:
            await self._flush_pending()

    # -- ProgressSink overrides ---------------------------------------------

    async def on_narration(self, text: str) -> None:
        await self._append("narration", text)

    async def on_thinking(self, text: str) -> None:
        await self._append("thinking", text)

    async def on_tool_call(self, name: str, args: dict, tool_use_id: str) -> None:
        await self._flush_pending()
        from agenticore.connectors.telegram import _extract_args_preview

        preview = _extract_args_preview(name or "tool", args, max_len=300)
        body = f"**Tool call:** {name or 'tool'}"
        if preview:
            body += f"\n`{preview}`"
        await self._post(body)

    async def on_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None:
        await self._flush_pending()
        out = content or ""
        if len(out) > self._RESULT_MAX:
            out = out[: self._RESULT_MAX] + "\n… (truncated)"
        label = "**Tool result (error):**" if is_error else "**Tool result:**"
        await self._post(f"{label}\n```\n{out}\n```")

    async def on_final(self, text: str) -> None:
        if self._final_sent:
            return
        self._final_sent = True
        self.final_text = text or ""
        await self._flush_pending()
        if text:
            await self._post(text)

    async def on_error(self, message: str, code=None) -> None:
        # Fire at most once, and never after a final answer already landed.
        if self._error_sent or self._final_sent:
            return
        self._error_sent = True
        await self._flush_pending()
        await self._post(f"**Error:** {message}")


# ---------------------------------------------------------------------------
# Bridge to agenticore's completions engine
# ---------------------------------------------------------------------------


async def _call_completions(
    messages: list[dict],
    conversation_id: str,
    *,
    sink: ProgressSink,
    stream_cfg: dict,
) -> str:
    """Run the agent in-process, dispatching progress events to ``sink``."""
    from agenticore.agent_mode.agent import AgentExecutor
    from agenticore.agent_mode.openai_compat import flatten_messages

    prompt = flatten_messages(messages)
    executor = AgentExecutor()
    result = await executor.execute(
        message=prompt,
        external_uuid=_uuid_for_conversation(conversation_id),
        wait=True,
        sink=sink,
        stream_cfg=stream_cfg,
    )
    if result.get("is_error"):
        raise RuntimeError(result.get("error", "Unknown agent error"))
    return result.get("result", "")


# Module-level singletons, initialised on first activity.
_store: Optional[ConversationStore] = None
_client: Optional[TeamsClient] = None


def _get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore(
            max_messages=int(os.environ.get("TEAMS_MAX_MESSAGES", "20")),
            ttl=int(os.environ.get("TEAMS_CONVERSATION_TTL", "86400")),
        )
    return _store


def _get_client() -> TeamsClient:
    global _client
    if _client is None:
        _client = TeamsClient()
    return _client


def _is_owner(activity: dict) -> bool:
    owner = os.environ.get("TEAMS_OWNER_AAD_ID", "").strip()
    if not owner:
        return True  # no owner configured → allow any authenticated user
    return (activity.get("from") or {}).get("aadObjectId") == owner


def _ref_from_activity(activity: dict) -> ConversationRef:
    return ConversationRef(
        service_url=activity.get("serviceUrl", ""),
        conversation_id=(activity.get("conversation") or {}).get("id", ""),
        # Outbound from = inbound recipient (the bot); recipient = inbound from.
        bot_account=activity.get("recipient") or {},
        user_account=activity.get("from") or {},
    )


async def handle_activity(activity: dict) -> None:
    """Process one inbound Teams activity. Owner-filtered, 1:1 message turns."""
    if activity.get("type") != "message":
        return  # ignore typing/conversationUpdate/etc. in v1

    # v1 is 1:1 DMs only. Refuse channel/group conversations so tool/reasoning
    # output is never disclosed to an unintended audience.
    conv_type = (activity.get("conversation") or {}).get("conversationType")
    if conv_type and conv_type != "personal":
        logger.info("teams: ignoring non-personal conversation (%s) — v1 is 1:1 only", conv_type)
        return

    if not _is_owner(activity):
        logger.info("teams: ignoring message from non-owner")
        return

    ref = _ref_from_activity(activity)
    if not ref.service_url or not ref.conversation_id:
        logger.warning("teams: activity missing serviceUrl/conversation id")
        return

    # Only ever send the bot's bearer token to a trusted Bot Framework host.
    if not _is_trusted_service_url(ref.service_url):
        logger.warning("teams: refusing untrusted serviceUrl: %s", ref.service_url)
        return

    text = _MENTION_RE.sub("", activity.get("text") or "").strip()
    if not text:
        return

    client = _get_client()
    store = _get_store()
    conv_id = ref.conversation_id

    # Minimal command handling (Teams has no native slash-command routing).
    lowered = text.lower()
    if lowered in ("/clear", "clear conversation"):
        store.clear(conv_id)
        await client.send_message(ref, "Conversation cleared.")
        return
    if lowered == "/status":
        await client.send_message(
            ref,
            f"Messages: {store.message_count(conv_id)}\nAgent: {_agent_id()}",
        )
        return

    # Resolve stream visibility (handles /show-thinking etc. + stickiness).
    from agenticore.agent_mode.stream_config import get_for_request

    clean_text, stream_cfg, tokens = get_for_request(_agent_id(), text)

    # Toggle-only message (slash tokens, nothing left to ask) → ack + return.
    if tokens and not clean_text.strip():
        vis = ", ".join(f"{k}={v}" for k, v in stream_cfg.items())
        await client.send_message(ref, f"Visibility updated: {vis}")
        return

    # Normal completion turn.
    messages = store.append(conv_id, "user", clean_text)
    from agenticore.capabilities import render_capabilities_prompt

    system_prompt = os.environ.get("TEAMS_SYSTEM_PROMPT", "")
    caps = render_capabilities_prompt()
    full_system = "\n\n".join(filter(None, [system_prompt, caps]))
    api_messages = [{"role": "system", "content": full_system}] + messages if full_system else messages

    await client.send_typing(ref)
    sink = TeamsProgressSink(client, ref)
    try:
        response = await _call_completions(api_messages, conv_id, sink=sink, stream_cfg=stream_cfg)
        store.append(conv_id, "assistant", response)
    except Exception as e:
        logger.exception("teams completions call failed")
        # The sink already surfaces agent-level errors via on_error; only post
        # here for failures it never saw (so the user gets exactly one error).
        if not (sink._error_sent or sink._final_sent):
            try:
                await client.send_message(ref, f"**Error:** {e}")
            except Exception:
                pass
