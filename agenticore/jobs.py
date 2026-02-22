"""Job store with Redis + file fallback.

Jobs are stored as Redis hashes or JSON files under ``~/.agenticore/jobs/``.
"""

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


@dataclass
class Job:
    id: str = ""
    repo_url: str = ""
    base_ref: str = "main"
    task: str = ""
    profile: str = "code"
    status: str = "queued"  # queued|running|succeeded|failed|cancelled|expired
    mode: str = "fire_and_forget"  # fire_and_forget|sync
    exit_code: Optional[int] = None
    session_id: Optional[str] = None  # Claude session ID (for resume)
    pr_url: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    ttl_seconds: int = 86400
    pid: Optional[int] = None  # OS process ID of claude subprocess

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        # Handle None values for optional fields
        return cls(
            id=data.get("id", ""),
            repo_url=data.get("repo_url", ""),
            base_ref=data.get("base_ref", "main"),
            task=data.get("task", ""),
            profile=data.get("profile", "code"),
            status=data.get("status", "queued"),
            mode=data.get("mode", "fire_and_forget"),
            exit_code=data.get("exit_code"),
            session_id=data.get("session_id"),
            pr_url=data.get("pr_url"),
            output=data.get("output"),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            ttl_seconds=int(data.get("ttl_seconds", 86400)),
            pid=data.get("pid"),
        )


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
        print(f"Redis connection failed: {exc}", file=sys.stderr)
        _redis_client = None

    return _redis_client


def _reset_redis():
    """Reset redis singleton (for testing)."""
    global _redis_client, _redis_checked
    _redis_client = None
    _redis_checked = False


def _redis_key(job_id: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
    return f"{prefix}:job:{job_id}"


# ---------------------------------------------------------------------------
# File fallback
# ---------------------------------------------------------------------------


def _jobs_dir() -> Path:
    d = Path.home() / ".agenticore" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_file(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_job(
    task: str,
    profile: str = "code",
    repo_url: str = "",
    base_ref: str = "main",
    mode: str = "fire_and_forget",
    session_id: Optional[str] = None,
    ttl_seconds: int = 86400,
) -> Job:
    """Create a new job and persist it."""
    job = Job(
        id=str(uuid.uuid4()),
        task=task,
        profile=profile,
        repo_url=repo_url,
        base_ref=base_ref,
        mode=mode,
        session_id=session_id,
        status="queued",
        created_at=_now_iso(),
        ttl_seconds=ttl_seconds,
    )
    _save_job(job)
    return job


def get_job(job_id: str) -> Optional[Job]:
    """Retrieve a job by ID."""
    r = _get_redis()
    if r is not None:
        data = r.hgetall(_redis_key(job_id))
        if data:
            # Convert redis string values
            if "exit_code" in data and data["exit_code"] != "None":
                data["exit_code"] = int(data["exit_code"])
            elif "exit_code" in data:
                data["exit_code"] = None
            if "ttl_seconds" in data:
                data["ttl_seconds"] = int(data["ttl_seconds"])
            if "pid" in data and data["pid"] != "None":
                data["pid"] = int(data["pid"])
            elif "pid" in data:
                data["pid"] = None
            return Job.from_dict(data)

    # File fallback
    path = _job_file(job_id)
    if path.exists():
        with open(path) as f:
            return Job.from_dict(json.load(f))

    return None


def update_job(job_id: str, **kwargs) -> Optional[Job]:
    """Update specific fields of a job."""
    job = get_job(job_id)
    if job is None:
        return None

    for key, value in kwargs.items():
        if hasattr(job, key):
            setattr(job, key, value)

    _save_job(job)
    return job


def list_jobs(limit: int = 20, status: Optional[str] = None) -> List[Job]:
    """List recent jobs, optionally filtered by status."""
    r = _get_redis()
    jobs: List[Job] = []

    if r is not None:
        prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
        # Scan for job keys
        cursor = 0
        keys = []
        while True:
            cursor, batch = r.scan(cursor, match=f"{prefix}:job:*", count=100)
            keys.extend(batch)
            if cursor == 0:
                break

        for key in keys:
            data = r.hgetall(key)
            if data:
                if "exit_code" in data and data["exit_code"] != "None":
                    data["exit_code"] = int(data["exit_code"])
                elif "exit_code" in data:
                    data["exit_code"] = None
                if "ttl_seconds" in data:
                    data["ttl_seconds"] = int(data["ttl_seconds"])
                if "pid" in data and data["pid"] != "None":
                    data["pid"] = int(data["pid"])
                elif "pid" in data:
                    data["pid"] = None
                jobs.append(Job.from_dict(data))
    else:
        # File fallback
        jobs_dir = _jobs_dir()
        for path in jobs_dir.glob("*.json"):
            with open(path) as f:
                jobs.append(Job.from_dict(json.load(f)))

    # Filter by status
    if status:
        jobs = [j for j in jobs if j.status == status]

    # Sort by created_at descending
    jobs.sort(key=lambda j: j.created_at, reverse=True)

    return jobs[:limit]


def cancel_job(job_id: str) -> Optional[Job]:
    """Cancel a running or queued job."""
    job = get_job(job_id)
    if job is None:
        return None

    if job.status not in ("queued", "running"):
        return job  # Already terminal

    # Kill process if running
    if job.pid is not None:
        try:
            os.kill(job.pid, 15)  # SIGTERM
        except ProcessLookupError:
            pass

    return update_job(job_id, status="cancelled", ended_at=_now_iso())


def _save_job(job: Job) -> None:
    """Persist a job to Redis and/or file."""
    r = _get_redis()
    data = job.to_dict()

    if r is not None:
        # Convert all values to strings for Redis hash
        str_data = {k: str(v) if v is not None else "None" for k, v in data.items()}
        r.hset(_redis_key(job.id), mapping=str_data)
        if job.ttl_seconds > 0:
            r.expire(_redis_key(job.id), job.ttl_seconds)

    # Always write file as fallback
    path = _job_file(job.id)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
