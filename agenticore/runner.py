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


def _build_env(cwd: Optional[Path] = None) -> dict:
    """Build full environment for the Claude subprocess."""
    env = os.environ.copy()
    env.update(_build_otel_env())

    cfg = get_config()
    if cfg.claude.config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude.config_dir

    if cfg.github.token:
        env["GITHUB_TOKEN"] = cfg.github.token

    return env


async def run_job(job: Job) -> Job:
    """Execute a job: clone repo, run Claude, handle result.

    Updates the job in-place and persists state changes.
    """
    cfg = get_config()

    # Load profile
    profile = get_profile(job.profile)
    if profile is None:
        return update_job(job.id, status="failed", error=f"Profile not found: {job.profile}", ended_at=_now_iso())

    # Mark as running
    update_job(job.id, status="running", started_at=_now_iso())
    trace = start_job_trace(job)

    cwd = None
    base_ref = job.base_ref

    # Clone repo if URL provided
    if job.repo_url:
        try:
            cwd = ensure_clone(job.repo_url)
            base_ref = base_ref or get_default_branch(cwd)
        except Exception as e:
            return update_job(job.id, status="failed", error=f"Clone failed: {e}", ended_at=_now_iso())

    # Materialize profile's .claude/ package into working directory
    if cwd:
        try:
            materialize_profile(profile, Path(cwd) if not isinstance(cwd, Path) else cwd)
        except Exception as e:
            return update_job(
                job.id, status="failed", error=f"Profile materialization failed: {e}", ended_at=_now_iso()
            )

    # Build template variables
    variables = {
        "TASK": job.task,
        "REPO_URL": job.repo_url or "",
        "BASE_REF": base_ref,
        "JOB_ID": job.id,
        "PROFILE": job.profile,
    }

    # Build CLI command
    cli_args = build_cli_args(profile, job.task, variables)
    cmd = [cfg.claude.binary] + cli_args

    # Handle resume
    if job.session_id:
        cmd.extend(["--resume", job.session_id])

    # Build environment
    env = _build_env(cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Store PID for cancellation
        update_job(job.id, pid=proc.pid)

        # Wait with timeout
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=profile.claude.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return update_job(
                job.id,
                status="failed",
                error=f"Timeout after {profile.claude.timeout}s",
                exit_code=-1,
                ended_at=_now_iso(),
            )

        exit_code = proc.returncode
        output_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        error_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        # Extract session_id from Claude's JSON output (last JSON line containing session_id)
        for line in reversed(output_text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    sid = data.get("session_id") or data.get("sessionId")
                    if sid:
                        update_job(job.id, session_id=sid)
                        break
                except json.JSONDecodeError:
                    pass

        status = "succeeded" if exit_code == 0 else "failed"

        job = update_job(
            job.id,
            status=status,
            exit_code=exit_code,
            output=output_text[:50000],  # Truncate large output
            error=error_text[:10000] if error_text else None,
            ended_at=_now_iso(),
        )

        # Auto-PR on success
        if status == "succeeded" and profile.auto_pr and job.repo_url:
            try:
                from agenticore.pr import create_auto_pr

                pr_url = await create_auto_pr(job)
                if pr_url:
                    job = update_job(job.id, pr_url=pr_url)
            except Exception as e:
                print(f"Auto-PR failed for job {job.id}: {e}", file=sys.stderr)

        return job

    except FileNotFoundError:
        return update_job(
            job.id,
            status="failed",
            error=f"Claude binary not found: {cfg.claude.binary}",
            ended_at=_now_iso(),
        )
    except Exception as e:
        return update_job(job.id, status="failed", error=str(e), ended_at=_now_iso())
    finally:
        final_job = get_job(job.id)
        ship_transcript(trace, getattr(final_job, "session_id", None), cwd=str(cwd) if cwd else None)
        end_job_trace(trace, final_job)


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
        asyncio.create_task(run_job(job))
        return job
