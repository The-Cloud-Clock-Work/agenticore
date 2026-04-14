"""Sticky per-agent stream visibility config + deterministic pseudo-slash filter.

Pseudo-slash tokens (`/show-thinking`, `/hide-tools`, etc.) are intercepted
in agenticore Python BEFORE the Claude subprocess spawns. They never reach
Claude. The toggle is sticky per agent_id (AGENTIHUB_AGENT) and persists
across calls in Redis (no TTL) with file fallback.

Defaults: assistant_text only. New agents see only the final assistant text;
thinking and tool events are opt-in via /show-thinking / /show-tools, and
the toggle persists until /hide-* or /hide-all flips it back.
"""

import json
import os
import re
from pathlib import Path

from agenticore.jobs import _get_redis

KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "agenticore")

DEFAULT_CONFIG: dict[str, bool] = {
    "show_thinking": True,
    "show_tools": True,
    "show_text": True,
}

SLASH_TOKENS: set[str] = {
    "/show-thinking",
    "/hide-thinking",
    "/show-tools",
    "/hide-tools",
    "/show-all",
    "/hide-all",
    "/stream-status",
}

_TOKEN_RE = re.compile(r"(?<!\S)(/[a-z][a-z-]*)(?!\S)")


def config_key(agent_id: str) -> str:
    return f"{KEY_PREFIX}:stream_config:{agent_id}"


def _config_file(agent_id: str) -> Path:
    home = Path(os.environ.get("HOME", "/root")) / ".agenticore" / "stream_config"
    return home / f"{agent_id}.json"


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def load_stream_config(agent_id: str) -> dict[str, bool]:
    """Load sticky config for agent. Falls back to file then DEFAULT_CONFIG."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hgetall(config_key(agent_id))
            if raw:
                decoded = {}
                for k, v in raw.items():
                    if isinstance(k, bytes):
                        k = k.decode()
                    if isinstance(v, bytes):
                        v = v.decode()
                    decoded[k] = v
                return {
                    "show_thinking": _coerce_bool(decoded.get("show_thinking", False)),
                    "show_tools": _coerce_bool(decoded.get("show_tools", False)),
                    "show_text": _coerce_bool(decoded.get("show_text", True)),
                }
        except Exception:
            pass
    f = _config_file(agent_id)
    if f.exists():
        try:
            data = json.loads(f.read_text())
            return {
                "show_thinking": _coerce_bool(data.get("show_thinking", False)),
                "show_tools": _coerce_bool(data.get("show_tools", False)),
                "show_text": _coerce_bool(data.get("show_text", True)),
            }
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_stream_config(agent_id: str, config: dict[str, bool]) -> None:
    """Persist config to Redis hash + file fallback. No TTL."""
    serialized = {
        "show_thinking": "1" if config.get("show_thinking", False) else "0",
        "show_tools": "1" if config.get("show_tools", False) else "0",
        "show_text": "1" if config.get("show_text", True) else "0",
    }
    r = _get_redis()
    if r is not None:
        try:
            key = config_key(agent_id)
            r.delete(key)
            r.hset(key, mapping=serialized)
        except Exception:
            pass
    try:
        f = _config_file(agent_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(config))
    except Exception:
        pass


def strip_tokens(message: str) -> tuple[str, list[str]]:
    """Pure function. Returns (cleaned_message, found_tokens).

    Matches whole-word tokens like /show-thinking. Unknown /tokens pass through.
    Strips matched tokens and collapses extra whitespace.
    """
    if not message:
        return message, []
    found: list[str] = []

    def _replace(m: re.Match) -> str:
        tok = m.group(1)
        if tok in SLASH_TOKENS:
            found.append(tok)
            return ""
        return tok

    cleaned = _TOKEN_RE.sub(_replace, message)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned, found


def apply_tokens(config: dict[str, bool], tokens: list[str]) -> dict[str, bool]:
    """Pure state machine. Returns NEW dict (does not mutate input)."""
    out = dict(config)
    for tok in tokens:
        if tok == "/show-thinking":
            out["show_thinking"] = True
        elif tok == "/hide-thinking":
            out["show_thinking"] = False
        elif tok == "/show-tools":
            out["show_tools"] = True
        elif tok == "/hide-tools":
            out["show_tools"] = False
        elif tok == "/show-all":
            out["show_thinking"] = True
            out["show_tools"] = True
            out["show_text"] = True
        elif tok == "/hide-all":
            out["show_thinking"] = False
            out["show_tools"] = False
            out["show_text"] = True
    return out


def get_for_request(
    agent_id: str,
    raw_message: str,
) -> tuple[str, dict[str, bool], list[str]]:
    """Top-level helper called by server.py.

    1. Load sticky config for agent_id.
    2. strip_tokens(raw_message) → (clean, found_tokens)
    3. If non-status tokens found: apply + save (sticky persisted).
    4. Returns (clean_message, resolved_config, found_tokens).
    """
    config = load_stream_config(agent_id)
    clean, tokens = strip_tokens(raw_message)
    if not tokens:
        return clean, config, tokens
    new_config = apply_tokens(config, tokens)
    mutating = [t for t in tokens if t != "/stream-status"]
    if mutating and new_config != config:
        save_stream_config(agent_id, new_config)
    return clean, new_config, tokens


def is_visible(event_type: str, config: dict[str, bool]) -> bool:
    """Pure predicate. Returns True if this event_type should be emitted."""
    if event_type == "thinking":
        return bool(config.get("show_thinking", False))
    if event_type in ("tool_use", "tool_result"):
        return bool(config.get("show_tools", False))
    if event_type == "assistant_text":
        return bool(config.get("show_text", True))
    if event_type == "done":
        return True
    if event_type == "stream_config":
        return True
    return False
