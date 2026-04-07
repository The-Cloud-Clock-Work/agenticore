"""Completion store with Redis + file fallback.

Completions track async agent requests through their lifecycle:
queued → running → completed/failed.

Redis queue (LPUSH/BRPOP) provides the work queue. Completion state
is stored as Redis hashes with file fallback.
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


@dataclass
class Completion:
    uuid: str = ""
    status: str = "queued"  # queued|running|completed|failed
    message: str = ""
    result: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    error: str = ""
    usage: dict = field(default_factory=dict)
    tool_uses: list = field(default_factory=list)
    callback_url: str = ""
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    request_params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Completion":
        usage = data.get("usage", {})
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except (json.JSONDecodeError, TypeError):
                usage = {}

        tool_uses = data.get("tool_uses", [])
        if isinstance(tool_uses, str):
            try:
                tool_uses = json.loads(tool_uses)
            except (json.JSONDecodeError, TypeError):
                tool_uses = []

        request_params = data.get("request_params", {})
        if isinstance(request_params, str):
            try:
                request_params = json.loads(request_params)
            except (json.JSONDecodeError, TypeError):
                request_params = {}

        return cls(
            uuid=data.get("uuid", ""),
            status=data.get("status", "queued"),
            message=data.get("message", ""),
            result=data.get("result", ""),
            session_id=data.get("session_id", ""),
            cost_usd=float(data.get("cost_usd", 0.0)),
            duration_ms=int(data.get("duration_ms", 0)),
            num_turns=int(data.get("num_turns", 0)),
            is_error=_to_bool(data.get("is_error", False)),
            error=data.get("error", ""),
            usage=usage,
            tool_uses=tool_uses,
            callback_url=data.get("callback_url", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            request_params=request_params,
        )


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

_redis_client = None
_redis_checked = False


def _get_redis():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True

    url = os.getenv("REDIS_URL", "")
    if not url:
        return None

    try:
        import redis as redis_lib

        _redis_client = redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=5.0)
        _redis_client.ping()
    except Exception as exc:
        print(f"Completions Redis connection failed: {exc}", file=sys.stderr)
        _redis_client = None

    return _redis_client


def _reset_redis():
    """Reset redis singleton (for testing)."""
    global _redis_client, _redis_checked
    _redis_client = None
    _redis_checked = False


def _key_prefix() -> str:
    return os.getenv("REDIS_KEY_PREFIX", "agenticore")


def _redis_key(uuid: str) -> str:
    return f"{_key_prefix()}:completion:{uuid}"


def _queue_key() -> str:
    return f"{_key_prefix()}:cq"


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def _completions_dir() -> Path:
    d = Path.home() / ".agenticore" / "completions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _completion_file(uuid: str) -> Path:
    return _completions_dir() / f"{uuid}.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_completion(
    uuid: str,
    message: str,
    callback_url: str = "",
    request_params: Optional[dict] = None,
) -> Completion:
    """Create a new completion and persist it."""
    completion = Completion(
        uuid=uuid,
        status="queued",
        message=message,
        callback_url=callback_url,
        created_at=_now_iso(),
        request_params=request_params or {},
    )
    _save_completion(completion)
    return completion


def enqueue_completion(completion: Completion, max_queue_depth: int = 0) -> Optional[dict]:
    """Push completion to Redis queue for worker processing.

    Returns None on success (queued to Redis).
    Returns the completion dict if Redis is unavailable (caller should
    handle inline execution).
    Returns {"rejected": True, ...} if queue depth exceeds max_queue_depth.
    """
    r = _get_redis()
    if r is not None:
        if max_queue_depth > 0:
            queue_len = r.llen(_queue_key())
            if queue_len >= max_queue_depth:
                return {"rejected": True, "error": "at capacity", "queue_depth": queue_len}
        payload = json.dumps(completion.to_dict())
        r.lpush(_queue_key(), payload)
        return None

    # No Redis — return dict for inline execution
    return completion.to_dict()


def dequeue_completion(timeout: int = 5) -> Optional[dict]:
    """Pop next completion from Redis queue (blocking).

    Returns parsed dict or None if queue is empty/timeout.
    """
    r = _get_redis()
    if r is None:
        return None

    result = r.brpop(_queue_key(), timeout=timeout)
    if result is None:
        return None

    _key, payload = result
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None


def get_completion(uuid: str) -> Optional[Completion]:
    """Retrieve a completion by UUID."""
    r = _get_redis()
    if r is not None:
        data = r.hgetall(_redis_key(uuid))
        if data:
            return Completion.from_dict(data)

    path = _completion_file(uuid)
    if path.exists():
        with open(path) as f:
            return Completion.from_dict(json.load(f))

    return None


def update_completion(uuid: str, **kwargs) -> Optional[Completion]:
    """Update specific fields of a completion."""
    completion = get_completion(uuid)
    if completion is None:
        return None

    for key, value in kwargs.items():
        if hasattr(completion, key):
            setattr(completion, key, value)

    _save_completion(completion)
    return completion


def list_completions(limit: int = 20, status: Optional[str] = None) -> List[Completion]:
    """List recent completions, optionally filtered by status."""
    r = _get_redis()
    completions = _load_from_redis(r) if r is not None else _load_from_files()

    if status:
        completions = [c for c in completions if c.status == status]

    completions.sort(key=lambda c: c.created_at, reverse=True)
    return completions[:limit]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _save_completion(completion: Completion) -> None:
    """Persist a completion to Redis and/or file."""
    r = _get_redis()
    data = completion.to_dict()

    if r is not None:
        str_data = {}
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                str_data[k] = json.dumps(v)
            elif isinstance(v, bool):
                str_data[k] = str(v).lower()
            else:
                str_data[k] = str(v) if v is not None else ""
        r.hset(_redis_key(completion.uuid), mapping=str_data)

        from agenticore.config import get_config

        ttl = get_config().agent_mode.session_ttl
        if ttl > 0:
            r.expire(_redis_key(completion.uuid), ttl)

    # Always write file as fallback
    path = _completion_file(completion.uuid)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _load_from_redis(r) -> List[Completion]:
    prefix = _key_prefix()
    cursor = 0
    keys = []
    while True:
        cursor, batch = r.scan(cursor, match=f"{prefix}:completion:*", count=100)
        keys.extend(batch)
        if cursor == 0:
            break

    completions = []
    for key in keys:
        data = r.hgetall(key)
        if data:
            completions.append(Completion.from_dict(data))
    return completions


def _load_from_files() -> List[Completion]:
    completions = []
    for path in _completions_dir().glob("*.json"):
        try:
            with open(path) as f:
                completions.append(Completion.from_dict(json.load(f)))
        except (json.JSONDecodeError, OSError):
            continue
    return completions
