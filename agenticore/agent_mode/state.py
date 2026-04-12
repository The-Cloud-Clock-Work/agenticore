"""Conversation state management for hooks integration.

Saves enriched state (uuid, wait mode, platform metadata) so that
hooks running inside Claude can read context about the current request.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


def _get_redis():
    from agenticore.jobs import _get_redis as jobs_redis

    return jobs_redis()


def _redis_key(external_uuid: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
    return f"{prefix}:agent_state:{external_uuid}"


def _state_file() -> Path:
    d = Path.home() / ".agenticore"
    d.mkdir(parents=True, exist_ok=True)
    return d / "conversation_map.json"


def _load_file_state() -> dict:
    path = _state_file()
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_file_state(all_state: dict) -> None:
    with open(_state_file(), "w") as f:
        json.dump(all_state, f, indent=2)


def save_state(
    external_uuid: str,
    wait: bool = True,
    meta: Optional[dict] = None,
) -> dict:
    """Save conversation state for a request.

    Returns the state dict that was persisted.
    """
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "uuid": external_uuid,
        "wait": wait,
        "meta": meta or {},
        "updated_at": now,
    }

    r = _get_redis()
    if r is not None:
        r.hset(
            _redis_key(external_uuid),
            mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in state.items()},
        )
        from agenticore.config import get_config

        ttl = get_config().agent_mode.session_ttl
        if ttl > 0:
            r.expire(_redis_key(external_uuid), ttl)

    all_state = _load_file_state()
    all_state[external_uuid] = state
    _save_file_state(all_state)

    return state


def get_session_state(external_uuid: str) -> Optional[dict]:
    """Read enriched state for a session."""
    r = _get_redis()
    if r is not None:
        data = r.hgetall(_redis_key(external_uuid))
        if data:
            result = {}
            for k, v in data.items():
                try:
                    result[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    result[k] = v
            return result

    all_state = _load_file_state()
    return all_state.get(external_uuid)
