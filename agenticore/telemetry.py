"""Langfuse telemetry for Agenticore jobs.

Non-fatal wrapper around the Langfuse SDK — all functions catch exceptions
and print to stderr rather than crashing the runner.
"""

import json
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Langfuse singleton
# ---------------------------------------------------------------------------

_langfuse = None
_langfuse_checked = False


def _get_langfuse():
    """Lazy Langfuse singleton. Returns None if not configured."""
    global _langfuse, _langfuse_checked
    if _langfuse_checked:
        return _langfuse
    _langfuse_checked = True

    import os

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        return None

    try:
        from langfuse import Langfuse

        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        # Pass Cloudflare Access headers via custom httpx client
        httpx_client = None
        cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "")
        cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "")
        if cf_id and cf_secret:
            import httpx

            httpx_client = httpx.Client(
                headers={
                    "CF-Access-Client-Id": cf_id,
                    "CF-Access-Client-Secret": cf_secret,
                }
            )

        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            httpx_client=httpx_client,
        )
    except Exception as exc:
        print(f"[telemetry] Langfuse init failed: {exc}", file=sys.stderr)
        _langfuse = None

    return _langfuse


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def encode_cwd_path(cwd: str) -> str:
    """Convert an absolute path to Claude project dir encoding.

    Example: /home/user/dev/proj  ->  -home-user-dev-proj
    """
    return cwd.replace("/", "-")


def _find_transcript(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """Locate a Claude session transcript JSONL file.

    Searches ~/.claude/projects/{encoded-cwd}/{session_id}.jsonl first,
    then falls back to scanning all project dirs.
    """
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None

    # Primary: use the known cwd
    if cwd:
        encoded = encode_cwd_path(str(cwd))
        candidate = claude_projects / encoded / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    # Fallback: scan all project dirs
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    return None


# ---------------------------------------------------------------------------
# Trace lifecycle
# ---------------------------------------------------------------------------


def start_job_trace(job):
    """Create a Langfuse trace for a job. Returns trace or None."""
    if job is None:
        return None
    lf = _get_langfuse()
    if lf is None:
        return None
    try:
        trace = lf.trace(
            id=job.id,
            name="agenticore-job",
            input={"task": job.task, "profile": job.profile, "repo_url": job.repo_url},
            metadata={"job_id": job.id, "base_ref": job.base_ref},
        )
        return trace
    except Exception as exc:
        print(f"[telemetry] start_job_trace failed: {exc}", file=sys.stderr)
        return None


def end_job_trace(trace, job) -> None:
    """Finalize a Langfuse trace with job outcome."""
    if trace is None or job is None:
        return
    try:
        trace.update(
            output={
                "status": job.status,
                "exit_code": job.exit_code,
                "pr_url": job.pr_url,
                "error": job.error,
            },
            metadata={
                "ended_at": job.ended_at,
                "started_at": job.started_at,
                "session_id": job.session_id,
            },
        )
        lf = _get_langfuse()
        if lf:
            lf.flush()
    except Exception as exc:
        print(f"[telemetry] end_job_trace failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Transcript shipping
# ---------------------------------------------------------------------------

_SKIP_TYPES = {"queue-operation", "file-history-snapshot", "progress"}


def _extract_block_text(block: dict) -> Optional[str]:
    """Extract text from a single content block."""
    if not isinstance(block, dict):
        return None
    btype = block.get("type", "")
    if btype == "text":
        text = block.get("text", "").strip()
        return text or None
    if btype == "tool_use":
        return f"[tool_use: {block.get('name', 'tool')}]"
    return None  # Skip tool_result, thinking, etc.


def _extract_turn_text(entry: dict) -> Optional[str]:
    """Extract readable text from a Claude transcript entry."""
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return None

    content = message.get("content", [])

    if isinstance(content, str):
        return content.strip() or None

    if not isinstance(content, list):
        return None

    parts = [t for block in content if (t := _extract_block_text(block))]
    return "\n".join(parts) if parts else None


def _is_tool_result_only(entry: dict) -> bool:
    """Check if entry contains only tool_result blocks."""
    message = entry.get("message", {})
    if not isinstance(message, dict):
        return False
    content = message.get("content", [])
    if not isinstance(content, list):
        return False
    return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content if isinstance(b, dict))


def _parse_transcript_entry(line: str):
    """Parse a JSONL line into (entry, entry_type) or None if skippable."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None

    entry_type = entry.get("type", "")
    if entry_type in _SKIP_TYPES or entry_type not in ("user", "assistant"):
        return None
    if _is_tool_result_only(entry):
        return None
    return entry, entry_type


def _ship_turn(trace, entry: dict, entry_type: str, index: int) -> None:
    """Ship a single transcript turn as a Langfuse span."""
    text = _extract_turn_text(entry)
    if not text:
        return
    try:
        trace.span(
            name=f"turn-{entry_type}-{index}",
            input=text if entry_type == "user" else None,
            output=text if entry_type == "assistant" else None,
            metadata={"turn_index": index, "role": entry_type},
        )
    except Exception:
        pass


def ship_transcript(trace, session_id: str, cwd: Optional[str] = None) -> None:
    """Read a Claude session JSONL and ship turns as Langfuse spans."""
    if trace is None or not session_id:
        return
    try:
        path = _find_transcript(session_id, cwd)
        if path is None:
            return

        with open(path, "r", errors="replace") as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            parsed = _parse_transcript_entry(line)
            if parsed is None:
                continue
            entry, entry_type = parsed
            _ship_turn(trace, entry, entry_type, i)

    except Exception as exc:
        print(f"[telemetry] ship_transcript failed: {exc}", file=sys.stderr)
