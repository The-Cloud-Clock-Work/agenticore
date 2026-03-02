"""Plan store with Redis + file fallback.

Plans are stored as Redis hashes or JSON files under ``~/.agenticore/plans/``.
A plan captures a Claude-generated markdown implementation plan that can later
be executed as a normal coding job.
"""

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


@dataclass
class Plan:
    id: str = ""
    name: str = ""  # human-readable slug, e.g. "fix-auth-bug-a3f2c1"
    task: str = ""  # original user request
    repo_url: str = ""
    content: str = ""  # markdown plan text (populated after job completes)
    status: str = "pending"  # pending | ready | failed
    created_at: str = ""
    planning_job_id: str = ""  # job that generated this plan
    execution_job_id: str = ""  # job that executed it (if any)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            task=data.get("task", ""),
            repo_url=data.get("repo_url", ""),
            content=data.get("content", ""),
            status=data.get("status", "pending"),
            created_at=data.get("created_at", ""),
            planning_job_id=data.get("planning_job_id", ""),
            execution_job_id=data.get("execution_job_id", ""),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_name(task: str) -> str:
    """Slugify first 5 words of task + 6-char UUID suffix."""
    words = re.sub(r"[^a-z0-9\s]", "", task.lower()).split()
    slug = "-".join(words[:5]) or "plan"
    suffix = str(uuid.uuid4())[:6]
    return f"{slug}-{suffix}"


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
        print(f"Redis connection failed: {exc}", file=sys.stderr)
        _redis_client = None

    return _redis_client


def _reset_redis():
    """Reset redis singleton (for testing)."""
    global _redis_client, _redis_checked
    _redis_client = None
    _redis_checked = False


def _redis_key(plan_id: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
    return f"{prefix}:plan:{plan_id}"


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def _plans_dir() -> Path:
    from agenticore.config import get_config

    cfg = get_config()
    if cfg.repos.shared_fs_root:
        d = Path(cfg.repos.shared_fs_root) / "plans"
    else:
        d = Path.home() / ".agenticore" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plan_file(plan_id: str) -> Path:
    return _plans_dir() / f"{plan_id}.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_plan(
    task: str,
    repo_url: str = "",
    planning_job_id: str = "",
) -> Plan:
    """Create a new plan and persist it."""
    plan = Plan(
        id=str(uuid.uuid4()),
        name=_plan_name(task),
        task=task,
        repo_url=repo_url,
        status="pending",
        created_at=_now_iso(),
        planning_job_id=planning_job_id,
    )
    _save_plan(plan)
    return plan


def get_plan(plan_id: str) -> Optional[Plan]:
    """Retrieve a plan by ID."""
    r = _get_redis()
    if r is not None:
        data = r.hgetall(_redis_key(plan_id))
        if data:
            return Plan.from_dict(data)

    # File fallback
    path = _plan_file(plan_id)
    if path.exists():
        with open(path) as f:
            return Plan.from_dict(json.load(f))

    return None


def update_plan(plan_id: str, **kwargs) -> Optional[Plan]:
    """Update specific fields of a plan."""
    plan = get_plan(plan_id)
    if plan is None:
        return None

    for key, value in kwargs.items():
        if hasattr(plan, key):
            setattr(plan, key, value)

    _save_plan(plan)
    return plan


def _scan_redis_plan_keys(r, prefix: str) -> list:
    """Scan Redis for all plan keys."""
    cursor = 0
    keys = []
    while True:
        cursor, batch = r.scan(cursor, match=f"{prefix}:plan:*", count=100)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


def list_plans(limit: int = 20) -> List[Plan]:
    """List recent plans sorted by created_at descending."""
    r = _get_redis()
    if r is not None:
        prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
        keys = _scan_redis_plan_keys(r, prefix)
        plans = []
        for key in keys:
            data = r.hgetall(key)
            if data:
                plans.append(Plan.from_dict(data))
    else:
        plans = []
        for path in _plans_dir().glob("*.json"):
            with open(path) as f:
                plans.append(Plan.from_dict(json.load(f)))

    plans.sort(key=lambda p: p.created_at, reverse=True)
    return plans[:limit]


def _save_plan(plan: Plan) -> None:
    """Persist a plan to Redis and/or file."""
    r = _get_redis()
    data = plan.to_dict()

    if r is not None:
        str_data = {k: str(v) if v is not None else "" for k, v in data.items()}
        r.hset(_redis_key(plan.id), mapping=str_data)
        # Plans don't expire — they are persistent user artifacts

    # Always write file as fallback
    path = _plan_file(plan.id)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
