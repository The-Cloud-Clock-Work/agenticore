"""Notification config management + HTTP delivery.

Manages per-request notification preferences (which event types to send)
and delivers notification payloads to callback URLs.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    callback_url: str = ""
    status: bool = True
    tool_call: bool = False
    thinking: bool = False
    enabled: bool = True  # global kill switch


# ---------------------------------------------------------------------------
# Redis helpers (reuse pattern from completions)
# ---------------------------------------------------------------------------


def _get_redis():
    from agenticore.agent_mode.completions import _get_redis as comp_redis

    return comp_redis()


def _redis_key(correlation_id: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
    return f"{prefix}:notification:{correlation_id}"


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def _config_file() -> Path:
    d = Path.home() / ".agenticore"
    d.mkdir(parents=True, exist_ok=True)
    return d / "notification_configs.json"


def _load_all_configs() -> dict:
    path = _config_file()
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_all_configs(configs: dict) -> None:
    with open(_config_file(), "w") as f:
        json.dump(configs, f, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_notification_config(correlation_id: str, config: NotificationConfig) -> None:
    """Persist notification config for a request."""
    data = asdict(config)

    r = _get_redis()
    if r is not None:
        str_data = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in data.items()}
        r.hset(_redis_key(correlation_id), mapping=str_data)

        from agenticore.config import get_config

        ttl = get_config().agent_mode.session_ttl
        if ttl > 0:
            r.expire(_redis_key(correlation_id), ttl)

    # Always write file fallback
    all_configs = _load_all_configs()
    all_configs[correlation_id] = data
    _save_all_configs(all_configs)


def get_notification_config(correlation_id: str) -> Optional[NotificationConfig]:
    """Load notification config for a request."""
    r = _get_redis()
    if r is not None:
        data = r.hgetall(_redis_key(correlation_id))
        if data:
            return _dict_to_config(data)

    all_configs = _load_all_configs()
    raw = all_configs.get(correlation_id)
    if raw:
        return _dict_to_config(raw)

    return None


def update_notification_config(correlation_id: str, **kwargs) -> Optional[NotificationConfig]:
    """Update specific fields of a notification config."""
    config = get_notification_config(correlation_id)
    if config is None:
        return None

    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)

    save_notification_config(correlation_id, config)
    return config


async def send_notification(correlation_id: str, event_type: str, payload: dict) -> bool:
    """Send a notification to the callback URL. Best-effort, never blocks.

    Returns True if delivered successfully, False otherwise.
    """
    config = get_notification_config(correlation_id)
    if config is None or not config.enabled or not config.callback_url:
        return False

    # Check if this event type is enabled
    if event_type in ("status", "result"):
        if not config.status:
            return False
    elif event_type == "tool_call":
        if not config.tool_call:
            return False
    elif event_type == "thinking":
        if not config.thinking:
            return False

    envelope = {
        "correlation_id": correlation_id,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }

    from agenticore.config import get_config

    timeout = get_config().agent_mode.notification_timeout

    try:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(config.callback_url, json=envelope, timeout=timeout)
            if resp.status_code < 400:
                return True
            _log.warning("Notification delivery failed (%d): %s", resp.status_code, config.callback_url)
            return False
    except Exception as exc:
        _log.debug("Notification delivery error: %s", exc)
        return False


def parse_notifications_param(notifications) -> dict:
    """Parse notifications from request body into a dict of bools.

    Accepts:
      - dict: {"status": true, "tool_call": true}
      - str: "status,tool_call"
    """
    if isinstance(notifications, dict):
        return {k: bool(v) for k, v in notifications.items() if k in ("status", "tool_call", "thinking")}

    if isinstance(notifications, str) and notifications:
        result = {}
        for item in notifications.split(","):
            item = item.strip()
            if item in ("status", "tool_call", "thinking"):
                result[item] = True
        return result

    return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def _dict_to_config(data: dict) -> NotificationConfig:
    return NotificationConfig(
        callback_url=data.get("callback_url", ""),
        status=_to_bool(data.get("status", True)),
        tool_call=_to_bool(data.get("tool_call", False)),
        thinking=_to_bool(data.get("thinking", False)),
        enabled=_to_bool(data.get("enabled", True)),
    )
