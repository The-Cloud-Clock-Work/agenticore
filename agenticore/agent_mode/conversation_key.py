"""4-tier conversation key resolver for sticky session routing.

Infers conversation identity from the request so callers from LibreChat,
raw curl, A2A, or any OpenAI-compat client can resume the same Claude
session across turns without per-client configuration.
"""

import hashlib
import logging
import re
from uuid import uuid4

_log = logging.getLogger(__name__)

SESSION_ID_HEADERS = [
    "x-conversation-id",
    "x-librechat-conversation-id",
    "x-openwebui-chat-id",
]

USER_ID_HEADERS = [
    "x-user-id",
    "x-openwebui-user-id",
]

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _extract_user_hint(headers: dict, body: dict) -> str:
    for h in USER_ID_HEADERS:
        val = headers.get(h, "").strip()
        if val:
            return val
    user = body.get("user", "")
    if isinstance(user, str) and user.strip():
        return user.strip()
    return ""


def _extract_system_prompt(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def _extract_first_user_content(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def hash_content_prefix(messages: list[dict]) -> str:
    system = _extract_system_prompt(messages)
    first_user = _extract_first_user_content(messages)
    raw = f"{system}\x00{first_user}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compose_storage_key(agent_id: str, user_hint: str, key: str) -> str:
    user_part = user_hint or "-"
    return f"conv:{agent_id}:{user_part}:{key}"


def resolve_conversation_key(
    headers: dict,
    body: dict,
    agent_id: str,
    *,
    hash_fallback: bool = True,
) -> tuple[str, str, str]:
    """Resolve a conversation key from the request.

    Returns (composed_storage_key, tier, user_hint).
    tier is one of: "header", "body", "content_hash", "ephemeral".
    """
    user_hint = _extract_user_hint(headers, body)
    messages = body.get("messages", [])

    for h in SESSION_ID_HEADERS:
        val = headers.get(h, "").strip()
        if val:
            key = compose_storage_key(agent_id, user_hint, val)
            _log.info("conversation_key tier=header header=%s key=%s", h, key)
            return key, "header", user_hint

    metadata = body.get("metadata", {})
    if isinstance(metadata, dict):
        conv_id = metadata.get("conversation_id", "")
        if isinstance(conv_id, str) and conv_id.strip():
            key = compose_storage_key(agent_id, user_hint, conv_id.strip())
            _log.info("conversation_key tier=body key=%s", key)
            return key, "body", user_hint

    user_field = body.get("user", "")
    if isinstance(user_field, str) and _is_uuid(user_field.strip()):
        key = compose_storage_key(agent_id, user_hint, user_field.strip())
        _log.info("conversation_key tier=body(user) key=%s", key)
        return key, "body", user_hint

    if hash_fallback and messages:
        first_user = _extract_first_user_content(messages)
        if first_user:
            content_hash = hash_content_prefix(messages)
            key = compose_storage_key(agent_id, user_hint, content_hash)
            _log.info("conversation_key tier=content_hash key=%s", key)
            return key, "content_hash", user_hint

    ephemeral = str(uuid4())
    key = compose_storage_key(agent_id, user_hint, ephemeral)
    _log.info("conversation_key tier=ephemeral key=%s", key)
    return key, "ephemeral", user_hint
