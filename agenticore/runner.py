"""Claude Code subprocess runner.

Spawns ``claude --worktree -p "task"`` with profile-derived flags and
OTEL environment variables. Manages the full job lifecycle: queued → running
→ succeeded/failed.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.jobs import Job, create_job, get_job, update_job
from agenticore.profiles import build_cli_args, get_profile, materialize_profile
from agenticore.repos import ensure_clone, get_default_branch
from agenticore.telemetry import end_job_trace, ship_transcript, start_job_trace

# Set to prevent GC of fire-and-forget background tasks
_background_tasks: set = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_otel_env() -> dict:
    """Build OTEL environment variables for the Claude subprocess."""
    cfg = get_config()
    if not cfg.otel.enabled:
        return {}

    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": cfg.otel.protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": cfg.otel.endpoint,
        "OTEL_LOG_USER_PROMPTS": "1" if cfg.otel.log_prompts else "0",
        "OTEL_LOG_TOOL_DETAILS": "1" if cfg.otel.log_tool_details else "0",
    }


def _build_env(_cwd: Optional[Path] = None) -> dict:
    """Build full environment for the Claude subprocess."""
    env = os.environ.copy()
    env.update(_build_otel_env())

    cfg = get_config()
    if cfg.claude.config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude.config_dir

    if cfg.github.token:
        env["GITHUB_TOKEN"] = cfg.github.token

    # Auto-build ANTHROPIC_CUSTOM_HEADERS for CF Access-protected proxies
    cf_id = env.get("CF_ACCESS_CLIENT_ID", "")
    cf_secret = env.get("CF_ACCESS_CLIENT_SECRET", "")
    if cf_id and cf_secret and "ANTHROPIC_CUSTOM_HEADERS" not in env:
        env["ANTHROPIC_CUSTOM_HEADERS"] = json.dumps(
            {
                "CF-Access-Client-Id": cf_id,
                "CF-Access-Client-Secret": cf_secret,
            }
        )

    return env


def _extract_session_id(output_text: str) -> Optional[str]:
    """Extract session_id from Claude's JSON output (last JSON line containing it)."""
    for line in reversed(output_text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            sid = data.get("session_id") or data.get("sessionId")
            if sid:
                return sid
        except json.JSONDecodeError:
            pass
    return None


def _prepare_job_repo(job: Job):
    """Clone repo and return (cwd, base_ref). Raises on failure."""
    cwd = ensure_clone(job.repo_url)
    base_ref = job.base_ref or get_default_branch(cwd)
    return cwd, base_ref


def _build_job_cmd(cfg, profile, job, base_ref, cwd):
    """Build the CLI command and environment for a job."""
    variables = {
        "TASK": job.task,
        "REPO_URL": job.repo_url or "",
        "BASE_REF": base_ref,
        "JOB_ID": job.id,
        "PROFILE": job.profile,
    }
    cli_args = build_cli_args(profile, job.task, variables)
    cmd = [cfg.claude.binary] + cli_args
    if job.session_id:
        cmd.extend(["--resume", job.session_id])
    env = _build_env(cwd)
    return cmd, env


async def _run_subprocess(job_id, cmd, cwd, env, timeout):
    """Run Claude subprocess with timeout. Returns (proc, stdout, stderr) or raises."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    update_job(job_id, pid=proc.pid)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc, stdout, stderr


async def _maybe_auto_pr(job: Job, profile, status: str) -> Job:
    """Create auto-PR if conditions are met."""
    if status != "succeeded" or not profile.auto_pr or not job.repo_url:
        return job
    try:
        from agenticore.pr import create_auto_pr

        pr_url = await create_auto_pr(job)
        if pr_url:
            job = update_job(job.id, pr_url=pr_url)
    except Exception as e:
        print(f"Auto-PR failed for job {job.id}: {e}", file=sys.stderr)
    return job


async def run_job(job: Job) -> Job:
    """Execute a job: clone repo, run Claude, handle result.

    Updates the job in-place and persists state changes.
    """
    cfg = get_config()

    profile = get_profile(job.profile)
    if profile is None:
        return update_job(job.id, status="failed", error=f"Profile not found: {job.profile}", ended_at=_now_iso())

    update_job(job.id, status="running", started_at=_now_iso())
    trace = start_job_trace(job)

    cwd = None
    base_ref = job.base_ref

    if job.repo_url:
        try:
            cwd, base_ref = _prepare_job_repo(job)
        except Exception as e:
            return update_job(job.id, status="failed", error=f"Clone failed: {e}", ended_at=_now_iso())

    if cwd:
        try:
            materialize_profile(profile, Path(cwd) if not isinstance(cwd, Path) else cwd)
        except Exception as e:
            return update_job(
                job.id, status="failed", error=f"Profile materialization failed: {e}", ended_at=_now_iso()
            )

    cmd, env = _build_job_cmd(cfg, profile, job, base_ref, cwd)

    try:
        job = await _execute_claude(job, cmd, cwd, env, profile, cfg)
        return job
    finally:
        final_job = get_job(job.id)
        ship_transcript(trace, getattr(final_job, "session_id", None), cwd=str(cwd) if cwd else None)
        end_job_trace(trace, final_job)


async def _execute_claude(job, cmd, cwd, env, profile, cfg):
    """Execute Claude subprocess and process results."""
    try:
        proc, stdout, stderr = await _run_subprocess(job.id, cmd, cwd, env, profile.claude.timeout)
    except asyncio.TimeoutError:
        return update_job(
            job.id,
            status="failed",
            error=f"Timeout after {profile.claude.timeout}s",
            exit_code=-1,
            ended_at=_now_iso(),
        )
    except FileNotFoundError:
        return update_job(
            job.id,
            status="failed",
            error=f"Claude binary not found: {cfg.claude.binary}",
            ended_at=_now_iso(),
        )
    except Exception as e:
        return update_job(job.id, status="failed", error=str(e), ended_at=_now_iso())

    output_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    error_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    sid = _extract_session_id(output_text)
    if sid:
        update_job(job.id, session_id=sid)

    status = "succeeded" if proc.returncode == 0 else "failed"
    job = update_job(
        job.id,
        status=status,
        exit_code=proc.returncode,
        output=output_text[:50000],
        error=error_text[:10000] if error_text else None,
        ended_at=_now_iso(),
    )

    return await _maybe_auto_pr(job, profile, status)


async def submit_job(
    task: str,
    profile: str = "code",
    repo_url: str = "",
    base_ref: str = "main",
    wait: bool = False,
    session_id: Optional[str] = None,
) -> Job:
    """Submit a new job for execution.

    Args:
        task: Task description
        profile: Profile name
        repo_url: Git repo URL (optional)
        base_ref: Base branch (default: main)
        wait: If True, wait for completion (sync mode)
        session_id: Claude session ID to resume

    Returns:
        The created Job (may still be running if wait=False)
    """
    cfg = get_config()

    mode = "sync" if wait else "fire_and_forget"
    job = create_job(
        task=task,
        profile=profile,
        repo_url=repo_url,
        base_ref=base_ref,
        mode=mode,
        session_id=session_id,
        ttl_seconds=cfg.repos.job_ttl_seconds,
    )

    if wait:
        # Synchronous: run and return completed job
        return await run_job(job)
    else:
        # Fire-and-forget: launch in background
        _background_task = asyncio.create_task(run_job(job))
        # Store reference to prevent GC of fire-and-forget task
        _background_tasks.add(_background_task)
        _background_task.add_done_callback(_background_tasks.discard)
        return job
