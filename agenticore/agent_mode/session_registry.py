"""Session registry: maps external UUID to Claude session ID.

Redis primary storage with JSON file fallback.
"""

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


class SessionType(str, Enum):
    PERSISTENT = "persistent"
    STATELESS = "stateless"


@dataclass
class SessionMapping:
    external_uuid: str
    claude_session_id: str
    session_type: str  # SessionType value
    status: str = "active"  # active, completed, failed
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMapping":
        return cls(
            external_uuid=data.get("external_uuid", ""),
            claude_session_id=data.get("claude_session_id", ""),
            session_type=data.get("session_type", SessionType.PERSISTENT.value),
            status=data.get("status", "active"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_redis():
    """Get Redis client from the jobs module (shared singleton)."""
    from agenticore.jobs import _get_redis as jobs_redis

    return jobs_redis()


def _redis_key(external_uuid: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
    return f"{prefix}:session:{external_uuid}"


def _sessions_file() -> Path:
    d = Path.home() / ".agenticore"
    d.mkdir(parents=True, exist_ok=True)
    return d / "agent_sessions.json"


def _load_file_sessions() -> dict:
    path = _sessions_file()
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_file_sessions(sessions: dict) -> None:
    path = _sessions_file()
    with open(path, "w") as f:
        json.dump(sessions, f, indent=2)


class SessionRegistry:
    """Maps external UUID to Claude session ID."""

    def register(self, external_uuid: str, stateless: bool = False) -> SessionMapping:
        """Register or retrieve a session mapping.

        For persistent sessions: reuses existing Claude session ID (enables --resume).
        For stateless sessions: always generates a fresh Claude session ID.
        """
        session_type = SessionType.STATELESS if stateless else SessionType.PERSISTENT

        if not stateless:
            existing = self.get(external_uuid)
            if existing and existing.status == "active":
                existing.updated_at = _now_iso()
                self._save(existing)
                return existing

        claude_session_id = external_uuid if not stateless else str(uuid.uuid4())
        now = _now_iso()
        mapping = SessionMapping(
            external_uuid=external_uuid,
            claude_session_id=claude_session_id,
            session_type=session_type.value,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._save(mapping)
        return mapping

    def get(self, external_uuid: str) -> Optional[SessionMapping]:
        """Look up a session mapping by external UUID."""
        r = _get_redis()
        if r is not None:
            data = r.hgetall(_redis_key(external_uuid))
            if data:
                return SessionMapping.from_dict(data)

        sessions = _load_file_sessions()
        if external_uuid in sessions:
            return SessionMapping.from_dict(sessions[external_uuid])
        return None

    def mark_complete(self, external_uuid: str) -> None:
        self._update_status(external_uuid, "completed")

    def mark_failed(self, external_uuid: str) -> None:
        self._update_status(external_uuid, "failed")

    def _update_status(self, external_uuid: str, status: str) -> None:
        mapping = self.get(external_uuid)
        if mapping:
            mapping.status = status
            mapping.updated_at = _now_iso()
            self._save(mapping)

    def _save(self, mapping: SessionMapping) -> None:
        data = mapping.to_dict()

        r = _get_redis()
        if r is not None:
            str_data = {k: str(v) for k, v in data.items()}
            r.hset(_redis_key(mapping.external_uuid), mapping=str_data)
            from agenticore.config import get_config

            ttl = get_config().agent_mode.session_ttl
            if ttl > 0:
                r.expire(_redis_key(mapping.external_uuid), ttl)

        sessions = _load_file_sessions()
        sessions[mapping.external_uuid] = data
        _save_file_sessions(sessions)


# Module-level singleton
_registry = SessionRegistry()


def register_session(external_uuid: str, stateless: bool = False) -> SessionMapping:
    return _registry.register(external_uuid, stateless=stateless)


def mark_session_complete(external_uuid: str) -> None:
    _registry.mark_complete(external_uuid)


def mark_session_failed(external_uuid: str) -> None:
    _registry.mark_failed(external_uuid)


def resolve_external_uuid(external_uuid: str) -> Optional[SessionMapping]:
    return _registry.get(external_uuid)
