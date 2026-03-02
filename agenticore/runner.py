"""Claude Code subprocess runner.

Spawns ``claude --worktree -p "task"`` with profile-derived flags and
OTEL environment variables. Manages the full job lifecycle: queued → running
→ succeeded/failed.
"""

import asyncio
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.jobs import Job, create_job, get_job, update_job
from agenticore.profiles import build_cli_args, get_profile, materialize_profile
from agenticore.repos import ensure_clone, get_default_branch, resolve_github_token
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


def _fetch_from_auth_broker(service: str, consumer_id: str = "agenticore") -> Optional[str]:
    """Fetch a credential string from Auth Broker. Returns None if unavailable."""
    from agenticore.auth_client import AuthClient  # lazy import

    client = AuthClient()
    if not client.enabled:
        return None
    return client.get_credential(service, consumer_id=consumer_id)


def _build_env(_cwd: Optional[Path] = None) -> dict:
    """Build full environment for the Claude subprocess.

    Credential resolution order:
      1. Auth Broker (AUTH_BROKER_URL) — Claude Max subscription token, direct Anthropic.
         When broker returns a token, ANTHROPIC_BASE_URL is cleared so the CLI hits
         Anthropic directly (not the LiteLLM proxy).
      2. Static env fallback — ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL as configured
         in the container (typically pointing at the LiteLLM proxy).
    """
    import logging

    _log = logging.getLogger(__name__)

    env = os.environ.copy()
    env.update(_build_otel_env())

    cfg = get_config()
    if cfg.claude.config_dir:
        env["CLAUDE_CONFIG_DIR"] = cfg.claude.config_dir

    if cfg.auth_broker.url:
        # Attempt Auth Broker — returns Claude Max subscription token
        key = _fetch_from_auth_broker("anthropic")
        if key:
            env["ANTHROPIC_API_KEY"] = key
            # Broker token is a real Anthropic credential — route directly,
            # not through the LiteLLM proxy
            env.pop("ANTHROPIC_BASE_URL", None)
            _log.debug("auth: using Auth Broker token (direct Anthropic)")
        else:
            # Broker configured but unavailable or no token — fall back to
            # static ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL from env (LiteLLM)
            _log.warning(
                "Auth Broker unreachable or returned no Anthropic token — "
                "falling back to static ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL"
            )

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


def _build_job_cmd(cfg, profile, job, base_ref, cwd, job_config_dir: Optional[Path] = None):
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
    if job_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(job_config_dir)
    return cmd, env


async def _run_subprocess(job_id, cmd, cwd, env):
    """Run Claude subprocess. Returns (proc, stdout, stderr)."""
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


def _inject_file_mcp(file_path: str, job_config_dir: Path) -> None:
    """Merge mcpServers from file_path into {job_config_dir}/.mcp.json.

    User-provided servers win on name collision.
    """
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"file_path not found: {file_path}")

    with open(src) as f:
        src_data = json.load(f)

    if "mcpServers" not in src_data:
        return  # not an MCP config, skip silently

    target = job_config_dir / ".mcp.json"
    target_data = json.loads(target.read_text()) if target.exists() else {}

    existing = target_data.get("mcpServers", {})
    existing.update(src_data["mcpServers"])  # file_path wins on collision
    target_data["mcpServers"] = existing

    with open(target, "w") as f:
        json.dump(target_data, f, indent=2)


async def run_job(job: Job) -> Job:
    """Execute a job: clone repo, run Claude, handle result.

    Updates the job in-place and persists state changes.
    """
    cfg = get_config()

    profile = get_profile(job.profile)
    if profile is None:
        return update_job(job.id, status="failed", error=f"Profile not found: {job.profile}", ended_at=_now_iso())

    # Record which pod is running this job
    pod_name = cfg.repos.pod_name or socket.gethostname()
    update_job(job.id, status="running", started_at=_now_iso(), pod_name=pod_name)
    trace = start_job_trace(job)

    cwd = None
    base_ref = job.base_ref
    job_config_dir: Optional[Path] = None

    if job.repo_url:
        try:
            cwd, base_ref = _prepare_job_repo(job)
        except Exception as e:
            return update_job(job.id, status="failed", error=f"Clone failed: {e}", ended_at=_now_iso())

        # Record worktree path (computed, not yet created — Claude creates it)
        repos_root = Path(cfg.repos.root)
        from agenticore.repos import _repo_key

        worktree_path = repos_root / _repo_key(job.repo_url) / "worktrees" / job.id
        update_job(job.id, worktree_path=str(worktree_path))

    try:
        job_config_dir = materialize_profile(profile, job_id=job.id)
        if job_config_dir:
            update_job(job.id, job_config_dir=str(job_config_dir))
    except Exception as e:
        return update_job(
            job.id,
            status="failed",
            error=f"Profile materialization failed: {e}",
            ended_at=_now_iso(),
        )

    if job.file_path:
        try:
            job_config_dir = _ensure_writable_config_dir(job.id, job_config_dir)
            _inject_file_mcp(job.file_path, job_config_dir)
            update_job(job.id, job_config_dir=str(job_config_dir))
        except Exception as e:
            return update_job(
                job.id,
                status="failed",
                error=f"MCP injection failed: {e}",
                ended_at=_now_iso(),
            )

    cmd, env = _build_job_cmd(cfg, profile, job, base_ref, cwd, job_config_dir=job_config_dir)

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


async def _run_plan_job(job: Job, plan) -> Job:
    """Execute a planning job and populate plan.content from output."""
    from agenticore.plans import update_plan

    cfg = get_config()
    pod_name = cfg.repos.pod_name or socket.gethostname()
    update_job(job.id, status="running", started_at=_now_iso(), pod_name=pod_name)

    cwd = None
    if job.repo_url:
        try:
            cwd, _ = _prepare_job_repo(job)
        except Exception as e:
            update_plan(plan.id, status="failed")
            return update_job(job.id, status="failed", error=f"Clone failed: {e}", ended_at=_now_iso())

    plan_config_dir = None
    if job.file_path:
        try:
            plan_config_dir = _ensure_writable_config_dir(job.id, None)
            _inject_file_mcp(job.file_path, plan_config_dir)
        except Exception as e:
            update_plan(plan.id, status="failed")
            return update_job(job.id, status="failed", error=f"MCP injection failed: {e}", ended_at=_now_iso())

    cmd, env = _build_plan_cmd(cfg, job)
    if plan_config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(plan_config_dir)

    try:
        async with asyncio.timeout(900):
            proc, stdout, stderr = await _run_subprocess(job.id, cmd, cwd, env)
    except asyncio.TimeoutError:
        update_plan(plan.id, status="failed")
        return update_job(job.id, status="failed", error="Planning timeout", exit_code=-1, ended_at=_now_iso())
    except FileNotFoundError:
        update_plan(plan.id, status="failed")
        return update_job(
            job.id, status="failed", error=f"Claude binary not found: {cfg.claude.binary}", ended_at=_now_iso()
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
    else:
        update_plan(plan.id, status="failed")

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
        t.add_done_callback(_background_tasks.discard)

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
        file_path=file_path,
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
