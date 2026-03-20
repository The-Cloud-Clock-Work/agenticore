"""Claude Code subprocess runner.

Spawns ``claude -p "task"`` in a bespoke worktree with profile-derived flags
and OTEL environment variables. Manages the full job lifecycle: queued →
running → succeeded/failed.
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logging

from agenticore.config import get_config
from agenticore.jobs import Job, create_job, get_job, update_job
from agenticore.profiles import build_cli_args, get_profile, materialize_profile
from agenticore.repos import (
    ensure_clone,
    ensure_repo_exists,
    get_default_branch,
    get_worktree,
    prepare_worktree,
    resolve_github_token,
    update_worktree,
)
from agenticore.telemetry import end_job_trace, ship_transcript, start_job_trace

logger = logging.getLogger(__name__)


# Set to prevent GC of fire-and-forget background tasks
_background_tasks: set = set()


def _task_done_callback(task: asyncio.Task) -> None:
    """Log unhandled exceptions from fire-and-forget tasks, then discard."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background task %s failed with unhandled exception: %s", task.get_name(), exc, exc_info=exc)


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


def _log_job_result(mgmt, job) -> None:
    """Write job completion to management log."""
    try:
        duration = 0
        if job.started_at and job.ended_at:
            from datetime import datetime

            start = datetime.fromisoformat(job.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(job.ended_at.replace("Z", "+00:00"))
            duration = int((end - start).total_seconds())
        if job.status == "succeeded":
            mgmt.info("job-done id=%s status=succeeded duration=%ds pr=%s", job.id, duration, job.pr_url or "-")
        else:
            err = (job.error or "")[:200]
            mgmt.warning(
                "job-fail id=%s status=%s duration=%ds exit=%s error=%s",
                job.id,
                job.status,
                duration,
                job.exit_code,
                err,
            )
    except Exception:
        pass  # never let mgmt logging break job flow


def _build_env(_cwd: Optional[Path] = None) -> dict:
    """Build full environment for the Claude subprocess.

    Auth uses CLAUDE_CODE_OAUTH_TOKEN (set externally as a secret) when present,
    otherwise falls back to ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL.
    """
    import logging

    _log = logging.getLogger(__name__)
    from agenticore.mgmt_log import get_mgmt_logger

    mgmt = get_mgmt_logger()

    env = os.environ.copy()
    env.update(_build_otel_env())

    cfg = get_config()
    env["CLAUDE_CODE_HOME_DIR"] = cfg.claude.claude_home_dir

    # CLAUDE_CODE_OAUTH_TOKEN is set externally (K8s secret / env var).
    # When present, remove API-key auth so Claude Code uses OAuth directly.
    oauth_token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if oauth_token:
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        _log.debug("auth: using CLAUDE_CODE_OAUTH_TOKEN from environment")
        mgmt.info("auth provider=anthropic-oauth")
    else:
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        mgmt.info("auth static provider=%s", "litellm" if base_url else "anthropic")

    # GitHub token — delegate to the centralized resolver
    gh_token = resolve_github_token()
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
    else:
        env.pop("GITHUB_TOKEN", None)

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


# Public alias for agent_mode and other modules that need the subprocess env
build_subprocess_env = _build_env


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


def _prepare_job_repo(job: Job, create_repo: bool = False, private: bool = True):
    """Clone repo and return (cwd, base_ref). Raises on failure."""
    if create_repo and job.repo_url:
        ensure_repo_exists(job.repo_url, private=private)
    cwd = ensure_clone(job.repo_url)
    base_ref = job.base_ref or get_default_branch(cwd)
    return cwd, base_ref


def _resolve_cwd(
    job: Job,
    worktree_id: str = "",
    create_repo: bool = False,
    private: bool = True,
) -> tuple[Optional[Path], str]:
    """Resolve the working directory for a job. Returns (cwd, base_ref).

    Priority:
    1. worktree_id provided → load existing worktree, use its path
    2. repo_url + profile.worktree=True → prepare_worktree() (Phase 1)
    3. repo_url + profile.worktree=False → ensure_clone() only
    4. No repo_url → None (run_job creates temp dir)
    """
    from agenticore.profiles import get_profile

    if worktree_id:
        wt = get_worktree(worktree_id)
        if wt is None:
            raise ValueError(f"Worktree not found: {worktree_id}")
        if wt.status not in ("ready", "in_use"):
            raise ValueError(f"Worktree {worktree_id} not usable (status: {wt.status})")
        wt_path = Path(wt.path)
        if not wt_path.is_dir():
            update_worktree(worktree_id, status="failed", error="Worktree directory missing from disk")
            raise ValueError(f"Worktree directory missing: {wt.path}")
        update_worktree(worktree_id, status="in_use")
        update_job(job.id, worktree_id=worktree_id, worktree_path=wt.path, branch=wt.branch)
        return wt_path, wt.base_ref or job.base_ref

    if not job.repo_url:
        return None, job.base_ref

    if create_repo:
        ensure_repo_exists(job.repo_url, private=private)

    profile = get_profile(job.profile)
    if profile and profile.claude.worktree:
        wt = prepare_worktree(job.repo_url, job.base_ref or "main")
        update_worktree(wt.id, status="in_use")
        update_job(job.id, worktree_id=wt.id, worktree_path=wt.path, branch=wt.branch)
        return Path(wt.path), wt.base_ref

    cwd = ensure_clone(job.repo_url)
    base_ref = job.base_ref or get_default_branch(cwd)
    return cwd, base_ref


def _build_job_cmd(cfg, profile, job, base_ref, cwd):
    """Build the CLI command and environment for a job."""
    import copy

    variables = {
        "TASK": job.task,
        "REPO_URL": job.repo_url or "",
        "BASE_REF": base_ref,
        "JOB_ID": job.id,
        "PROFILE": job.profile,
    }
    # Never pass --worktree to Claude — agenticore manages worktrees itself
    cli_profile = copy.deepcopy(profile)
    cli_profile.claude.worktree = False
    cli_args = build_cli_args(cli_profile, job.task, variables)
    cmd = [cfg.claude.binary] + cli_args
    if job.session_id:
        cmd.extend(["--resume", job.session_id])
    env = _build_env(cwd)
    return cmd, env


async def _run_subprocess(job_id, cmd, cwd, env):
    """Run Claude subprocess. Returns (proc, stdout, stderr).

    Validates CWD exists before exec. On NFS, retries up to 10 times
    (1s apart) with parent-dir invalidation to bust attribute cache.
    """
    if cwd is not None:
        cwd_path = Path(cwd) if not isinstance(cwd, Path) else cwd
        for attempt in range(10):
            # Force NFS attribute cache invalidation by listing parent dir
            try:
                os.listdir(cwd_path.parent)
            except OSError:
                pass
            if cwd_path.is_dir():
                break
            logger.warning("CWD not visible (attempt %d/10): %s", attempt + 1, cwd_path)
            await asyncio.sleep(1.0)
        else:
            raise FileNotFoundError(f"CWD does not exist after 10 retries (NFS?): {cwd_path}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    update_job(job_id, pid=proc.pid)
    stdout, stderr = await proc.communicate()
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


def _ensure_writable_config_dir(job_id: str, existing_config_dir: Optional[Path]) -> Path:
    """Return a writable per-job config dir, creating it if necessary.

    If materialize_profile already created a job-specific dir (the merge path),
    it already exists and is returned as-is. If the profile fast-path was used
    (shared profile dir, not writable), a new job dir is created and profile
    contents are copied in.
    """
    cfg = get_config()
    if cfg.repos.shared_fs_root:
        job_dir = Path(cfg.repos.shared_fs_root) / "jobs" / job_id
    else:
        job_dir = Path("/tmp") / "agenticore-jobs" / job_id

    if job_dir.exists():
        return job_dir  # materialize_profile already created it (merge path)

    job_dir.mkdir(parents=True, exist_ok=True)
    if existing_config_dir and existing_config_dir.exists():
        shutil.copytree(existing_config_dir, job_dir, dirs_exist_ok=True)
    return job_dir


def _inject_file_mcp(file_path: str, cwd: Path) -> None:
    """Merge mcpServers from file_path into {cwd}/.mcp.json.

    Claude reads .mcp.json from the working directory (CWD).
    User-provided servers win on name collision.
    """
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"file_path not found: {file_path}")

    with open(src) as f:
        src_data = json.load(f)

    if "mcpServers" not in src_data:
        return  # not an MCP config, skip silently

    target = cwd / ".mcp.json"
    target_data = json.loads(target.read_text()) if target.exists() else {}

    existing = target_data.get("mcpServers", {})
    existing.update(src_data["mcpServers"])  # file_path wins on collision
    target_data["mcpServers"] = existing

    with open(target, "w") as f:
        json.dump(target_data, f, indent=2)


def _inject_mcp_configs(job: Job, cfg, mcp_cwd: Optional[Path]):
    """Inject MCP configs into working directory. Returns a failed Job on error, else None."""
    if not mcp_cwd:
        return None
    default_mcp = cfg.claude.default_mcp_file
    if default_mcp and Path(default_mcp).exists():
        try:
            _inject_file_mcp(default_mcp, mcp_cwd)
        except Exception as e:
            logger.warning("Default MCP injection failed: %s", e)
    if job.file_path:
        try:
            _inject_file_mcp(job.file_path, mcp_cwd)
        except Exception as e:
            return update_job(job.id, status="failed", error=f"MCP injection failed: {e}", ended_at=_now_iso())
    return None


async def run_job(
    job: Job,
    cwd: Optional[Path] = None,
    base_ref: str = "",
    create_repo: bool = False,
    private: bool = True,
    worktree_id: str = "",
) -> Job:
    """Execute a job in a pre-resolved working directory.

    If cwd is not provided, resolves it via _resolve_cwd() (backward compat).
    Updates the job in-place and persists state changes.
    """
    cfg = get_config()

    profile = get_profile(job.profile)
    if profile is None:
        return update_job(job.id, status="failed", error=f"Profile not found: {job.profile}", ended_at=_now_iso())

    pod_name = cfg.repos.pod_name or socket.gethostname()
    update_job(job.id, status="running", started_at=_now_iso(), pod_name=pod_name)
    trace = start_job_trace(job)

    from agenticore.mgmt_log import get_mgmt_logger

    mgmt = get_mgmt_logger()
    mgmt.info("job-start id=%s profile=%s repo=%s pod=%s", job.id, job.profile, job.repo_url or "-", pod_name)

    # Resolve CWD if not provided by caller
    if cwd is None:
        try:
            cwd, base_ref = _resolve_cwd(job, worktree_id=worktree_id, create_repo=create_repo, private=private)
        except Exception as e:
            return update_job(job.id, status="failed", error=f"CWD resolution failed: {e}", ended_at=_now_iso())

    if not base_ref:
        base_ref = job.base_ref

    try:
        profile_dir = materialize_profile(profile, job_id=job.id)
        if profile_dir:
            update_job(job.id, job_config_dir=str(profile_dir))
    except Exception as e:
        return update_job(
            job.id,
            status="failed",
            error=f"Profile materialization failed: {e}",
            ended_at=_now_iso(),
        )

    # For no-repo jobs without a pre-resolved cwd, create temp dir for MCP injection
    if cwd is None:
        shared = cfg.repos.shared_fs_root or tempfile.gettempdir()
        no_repo_cwd = Path(shared) / "jobs" / job.id / "cwd"
        no_repo_cwd.mkdir(parents=True, exist_ok=True)
        cwd = no_repo_cwd

    failed = _inject_mcp_configs(job, cfg, cwd)
    if failed:
        return failed

    cmd, env = _build_job_cmd(cfg, profile, job, base_ref, cwd)

    try:
        job = await _execute_claude(job, cmd, cwd, env, profile, cfg, rdir=cwd)
        return job
    finally:
        final_job = get_job(job.id)
        ship_transcript(trace, getattr(final_job, "session_id", None), cwd=str(cwd) if cwd else None)
        end_job_trace(trace, final_job)
        _log_job_result(mgmt, final_job)


async def _execute_claude(job, cmd, cwd, env, profile, cfg, rdir=None):
    """Execute Claude subprocess and process results."""
    try:
        async with asyncio.timeout(profile.claude.timeout):
            proc, stdout, stderr = await _run_subprocess(job.id, cmd, cwd, env)
    except asyncio.TimeoutError:
        return update_job(
            job.id,
            status="failed",
            error=f"Timeout after {profile.claude.timeout}s",
            exit_code=-1,
            ended_at=_now_iso(),
        )
    except FileNotFoundError as exc:
        cwd_exists = Path(cwd).exists() if cwd else "N/A"
        binary_found = shutil.which(cfg.claude.binary)
        return update_job(
            job.id,
            status="failed",
            error=(
                f"FileNotFoundError: {exc} | "
                f"filename={exc.filename} | "
                f"cwd_exists={cwd_exists} | "
                f"binary_at={binary_found}"
            ),
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


_PLAN_PROMPT_TEMPLATE = (
    "Create a detailed implementation plan for the following task:\n\n"
    "{task}\n\n"
    "Analyse the codebase thoroughly. Your plan must include:\n"
    "1. Problem analysis\n"
    "2. Files to change and why\n"
    "3. Step-by-step implementation\n"
    "4. Edge cases and risks\n"
    "5. Testing strategy\n\n"
    "Output the complete plan in markdown. DO NOT modify any files."
)


def _build_plan_cmd(cfg, job):
    """Build CLI command for a planning job — read-only, text output."""
    prompt = _PLAN_PROMPT_TEMPLATE.format(task=job.task)
    cmd = [
        cfg.claude.binary,
        "--output-format",
        "text",
        "--max-turns",
        "15",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--allowedTools",
        "Read,Glob,Grep",
        "-p",
        prompt,
    ]
    env = _build_env()
    return cmd, env


def _prepare_plan_env(job: Job):
    """Prepare cwd and config dir for a plan job. Returns (cwd, config_dir) or raises."""
    cwd = None
    if job.repo_url:
        cwd, _ = _prepare_job_repo(job)
    config_dir = None
    if job.file_path:
        config_dir = _ensure_writable_config_dir(job.id, None)
        _inject_file_mcp(job.file_path, config_dir)
    return cwd, config_dir


async def _run_plan_job(job: Job, plan) -> Job:
    """Execute a planning job and populate plan.content from output."""
    from agenticore.plans import update_plan

    cfg = get_config()
    pod_name = cfg.repos.pod_name or socket.gethostname()
    update_job(job.id, status="running", started_at=_now_iso(), pod_name=pod_name)

    from agenticore.mgmt_log import get_mgmt_logger

    mgmt = get_mgmt_logger()
    mgmt.info("plan-start id=%s plan=%s repo=%s pod=%s", job.id, plan.id, job.repo_url or "-", pod_name)

    try:
        cwd, plan_config_dir = _prepare_plan_env(job)
    except Exception as e:
        update_plan(plan.id, status="failed")
        return update_job(job.id, status="failed", error=f"Plan setup failed: {e}", ended_at=_now_iso())

    cmd, env = _build_plan_cmd(cfg, job)

    try:
        async with asyncio.timeout(900):
            proc, stdout, stderr = await _run_subprocess(job.id, cmd, cwd, env)
    except asyncio.TimeoutError:
        update_plan(plan.id, status="failed")
        return update_job(job.id, status="failed", error="Planning timeout", exit_code=-1, ended_at=_now_iso())
    except FileNotFoundError as exc:
        cwd_exists = Path(cwd).exists() if cwd else "N/A"
        binary_found = shutil.which(cfg.claude.binary)
        update_plan(plan.id, status="failed")
        return update_job(
            job.id,
            status="failed",
            error=(
                f"FileNotFoundError: {exc} | "
                f"filename={exc.filename} | "
                f"cwd_exists={cwd_exists} | "
                f"binary_at={binary_found}"
            ),
            ended_at=_now_iso(),
        )
    except Exception as e:
        update_plan(plan.id, status="failed")
        return update_job(job.id, status="failed", error=str(e), ended_at=_now_iso())

    output_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    error_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    status = "succeeded" if proc.returncode == 0 else "failed"

    job = update_job(
        job.id,
        status=status,
        exit_code=proc.returncode,
        output=output_text[:50000],
        error=error_text[:10000] if error_text else None,
        ended_at=_now_iso(),
    )

    if status == "succeeded" and output_text.strip():
        update_plan(plan.id, content=output_text.strip(), status="ready")
        mgmt.info("plan-done id=%s plan=%s status=ready", job.id, plan.id)
    else:
        update_plan(plan.id, status="failed")
        mgmt.warning("plan-fail id=%s plan=%s exit=%s", job.id, plan.id, proc.returncode)

    return job


async def submit_plan_job(
    task: str,
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> tuple:
    """Submit a planning job (read-only Claude run).

    Returns:
        Tuple of (Job, Plan)
    """
    from agenticore.plans import create_plan

    cfg = get_config()
    mode = "sync" if wait else "fire_and_forget"
    job = create_job(
        task=task,
        profile="planning",
        repo_url=repo_url,
        base_ref="main",
        mode=mode,
        ttl_seconds=cfg.repos.job_ttl_seconds,
        file_path=file_path,
    )
    plan = create_plan(task=task, repo_url=repo_url, planning_job_id=job.id)

    if wait:
        job = await _run_plan_job(job, plan)
    else:
        t = asyncio.create_task(_run_plan_job(job, plan))
        _background_tasks.add(t)
        t.add_done_callback(_task_done_callback)

    return job, plan


async def execute_plan_job(
    plan_id: str,
    repo_url: str = "",
    profile: str = "",
    wait: bool = False,
    file_path: str = "",
) -> Job:
    """Execute a ready plan by submitting a normal coding job with plan content as context.

    Args:
        plan_id: Plan ID returned by submit_plan_job
        repo_url: Override repo URL (defaults to the one used when planning)
        profile: Execution profile (default: from config)
        wait: Block until execution completes

    Returns:
        The created execution Job
    """
    from agenticore.plans import get_plan, update_plan

    plan = get_plan(plan_id)
    if plan is None:
        raise ValueError(f"Plan not found: {plan_id}")
    if plan.status != "ready":
        raise ValueError(f"Plan {plan_id} not ready (status: {plan.status})")

    cfg = get_config()
    resolved_profile = profile or cfg.claude.default_profile
    task = (
        f"Execute the following implementation plan:\n\n"
        f"---\n{plan.content}\n---\n\n"
        f"Implement every step in the plan above. Work top to bottom."
    )
    job = await submit_job(
        task=task,
        profile=resolved_profile,
        repo_url=repo_url or plan.repo_url,
        wait=wait,
        file_path=file_path,
    )
    update_plan(plan_id, execution_job_id=job.id)
    return job


async def submit_job(
    task: str,
    profile: str = "code",
    repo_url: str = "",
    base_ref: str = "main",
    wait: bool = False,
    session_id: Optional[str] = None,
    file_path: str = "",
    create_repo: bool = False,
    private: bool = True,
    worktree_id: str = "",
) -> Job:
    """Submit a new job for execution.

    Args:
        task: Task description
        profile: Profile name
        repo_url: Git repo URL (optional)
        base_ref: Base branch (default: main)
        wait: If True, wait for completion (sync mode)
        session_id: Claude session ID to resume
        file_path: Path to .mcp.json to inject
        create_repo: Auto-create GitHub repo if it doesn't exist
        private: Create repo as private (default: True)
        worktree_id: Reuse an existing pre-prepared worktree (two-phase workflow)

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
        file_path=file_path,
    )

    if wait:
        return await run_job(job, create_repo=create_repo, private=private, worktree_id=worktree_id)
    else:
        _background_task = asyncio.create_task(
            run_job(job, create_repo=create_repo, private=private, worktree_id=worktree_id)
        )
        _background_tasks.add(_background_task)
        _background_task.add_done_callback(_task_done_callback)
        return job
