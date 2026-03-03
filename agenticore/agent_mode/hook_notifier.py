#!/usr/bin/env python3
"""Self-contained notification hook for Claude Code hooks.

Runs inside Claude's sandbox. Zero agenticore imports — uses only stdlib
plus optional redis package.

Reads hook event JSON from stdin, maps to notification type, and POSTs
to the callback URL configured for this correlation ID.

Hook triggers:
  - PostToolUse → tool_call notification
  - SubprocessOutputLine (thinking) → thinking notification
  - Notification → status notification

Env vars:
  - AGENTICORE_CORRELATION_ID: Request correlation ID
  - REDIS_URL: Redis connection string (optional)
  - REDIS_KEY_PREFIX: Key prefix (default: agenticore)
"""

import json
import os
import sys
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config_redis(correlation_id: str) -> dict | None:
    """Try loading notification config from Redis."""
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        return None

    try:
        import redis

        prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
        key = f"{prefix}:notification:{correlation_id}"
        r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2.0)
        data = r.hgetall(key)
        return data if data else None
    except Exception:
        return None


def _load_config_file(correlation_id: str) -> dict | None:
    """Try loading notification config from file fallback."""
    from pathlib import Path

    config_path = Path.home() / ".agenticore" / "notification_configs.json"
    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            all_configs = json.load(f)
        return all_configs.get(correlation_id)
    except (json.JSONDecodeError, OSError):
        return None


def _is_enabled(config: dict, event_type: str) -> bool:
    """Check if a notification type is enabled in the config."""
    enabled = config.get("enabled", "true")
    if str(enabled).lower() in ("false", "0", "no"):
        return False

    val = config.get(event_type, "false")
    return str(val).lower() in ("true", "1", "yes")


def _map_hook_event(hook_name: str, event: dict) -> tuple[str | None, dict]:
    """Map a Claude Code hook event to notification type + payload.

    Returns (event_type, payload) or (None, {}) if not mappable.
    """
    if hook_name == "PostToolUse":
        tool_name = event.get("tool_name", "")
        # Skip system/internal tools
        if tool_name.startswith("system/"):
            return None, {}
        return "tool_call", {
            "tool_name": tool_name,
            "file_path": event.get("file_path", ""),
            "description": event.get("description", ""),
        }

    if hook_name == "SubprocessOutputLine":
        # Only forward thinking-type output
        if event.get("type") == "thinking":
            return "thinking", {"content": event.get("content", "")}
        return None, {}

    if hook_name == "Notification":
        return "status", {"message": event.get("message", "")}

    return None, {}


def _send_notification(callback_url: str, correlation_id: str, event_type: str, payload: dict) -> None:
    """POST notification to callback URL using stdlib urllib."""
    import urllib.request
    import urllib.error

    envelope = {
        "correlation_id": correlation_id,
        "event_type": event_type,
        "timestamp": _now_iso(),
        "data": payload,
    }

    body = json.dumps(envelope).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Best-effort — never fail the hook


def main() -> None:
    """Hook entry point. Reads event from stdin, sends notification."""
    correlation_id = os.getenv("AGENTICORE_CORRELATION_ID", "")
    if not correlation_id:
        sys.exit(0)

    # Read hook event from stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        event = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    # Determine hook name from event or env
    hook_name = event.get("hook_name", os.getenv("CLAUDE_HOOK_NAME", ""))
    if not hook_name:
        sys.exit(0)

    # Load notification config
    config = _load_config_redis(correlation_id)
    if config is None:
        config = _load_config_file(correlation_id)
    if config is None:
        sys.exit(0)

    callback_url = config.get("callback_url", "")
    if not callback_url:
        sys.exit(0)

    # Map event to notification type
    event_type, payload = _map_hook_event(hook_name, event)
    if event_type is None:
        sys.exit(0)

    # Check if this type is enabled
    if not _is_enabled(config, event_type):
        sys.exit(0)

    # Send it
    _send_notification(callback_url, correlation_id, event_type, payload)


if __name__ == "__main__":
    main()
