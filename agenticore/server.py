"""Agenticore MCP Server + REST API.

5 MCP tools for AI clients, mirrored as REST endpoints.

Tools:
    - run_task      — Submit a task (repo_url, task, profile, wait, session_id)
    - get_job       — Job status, output, PR URL
    - list_jobs     — Recent jobs with status
    - cancel_job    — Cancel a running job
    - list_profiles — Available execution profiles
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from agenticore.config import get_config

logger = logging.getLogger(__name__)


def _make_mcp() -> FastMCP:
    cfg = get_config()
    return FastMCP(
        "agenticore",
        host=cfg.server.host,
        port=cfg.server.port,
        json_response=True,
    )


mcp = _make_mcp()


# ── MCP Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def run_task(
    task: str,
    repo_url: str = "",
    profile: str = "",
    base_ref: str = "main",
    session_id: str = "",
    file_path: str = "",
    create_repo: bool = False,
    private: bool = True,
    worktree_id: str = "",
    wait: bool = False,
) -> str:
    """Submit a task for Claude Code execution.

    Async (fire and forget) by default — returns job ID immediately.
    Set wait=True to block until the job completes.

    Args:
        task: What Claude should do
        repo_url: GitHub repo URL to clone (optional — omit for local tasks)
        profile: Execution profile name (default: auto-routed)
        base_ref: Base branch (default: main)
        session_id: Claude session ID to resume (optional)
        file_path: Path to a .mcp.json on the shared FS to inject into the job config (optional)
        create_repo: Auto-create GitHub repo if it doesn't exist (default: false)
        private: Create repo as private (default: true)
        worktree_id: Reuse a pre-prepared worktree (from prepare_worktree)
        wait: If true, block until job completes (default: false)

    Returns:
        JSON with job_id, status, and (if wait=true) output
    """
    try:
        from agenticore.router import route
        from agenticore.runner import submit_job

        resolved_profile = route(profile=profile, repo_url=repo_url)

        job = await submit_job(
            task=task,
            profile=resolved_profile,
            repo_url=repo_url,
            base_ref=base_ref,
            wait=wait,
            session_id=session_id or None,
            file_path=file_path,
            create_repo=create_repo,
            private=private,
            worktree_id=worktree_id,
        )

        if job.status == "rejected":
            return json.dumps({"success": False, "error": "at capacity", "retry": True, "job": job.to_dict()})
        return json.dumps({"success": True, "job": job.to_dict()})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_job(job_id: str) -> str:
    """Get job status, output, and artifacts.

    Args:
        job_id: Job UUID

    Returns:
        JSON with job details including status, exit_code, output, pr_url
    """
    try:
        from agenticore.jobs import get_job as _get_job

        job = _get_job(job_id)
        if job is None:
            return json.dumps({"success": False, "error": f"Job not found: {job_id}"})

        return json.dumps({"success": True, "job": job.to_dict()})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_jobs(limit: int = 20, status: str = "") -> str:
    """List recent jobs with status.

    Args:
        limit: Max jobs to return (default: 20)
        status: Filter by status (queued/running/succeeded/failed/cancelled)

    Returns:
        JSON with jobs list
    """
    try:
        from agenticore.jobs import list_jobs as _list_jobs

        jobs = _list_jobs(limit=limit, status=status or None)

        return json.dumps(
            {
                "success": True,
                "count": len(jobs),
                "jobs": [j.to_dict() for j in jobs],
            }
        )

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def cancel_job(job_id: str) -> str:
    """Cancel a running or queued job.

    Args:
        job_id: Job UUID to cancel

    Returns:
        JSON with updated job status
    """
    try:
        from agenticore.jobs import cancel_job as _cancel_job

        job = _cancel_job(job_id)
        if job is None:
            return json.dumps({"success": False, "error": f"Job not found: {job_id}"})

        return json.dumps({"success": True, "job": job.to_dict()})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def plan_task(
    task: str,
    repo_url: str = "",
    wait: bool = False,
    file_path: str = "",
) -> str:
    """Create an implementation plan without executing it.

    Runs Claude in read-only mode (Read, Glob, Grep only) to analyse the
    codebase and produce a markdown plan. The plan can later be executed
    with execute_plan.

    Args:
        task: What to plan (same format as run_task)
        repo_url: Repo to analyse (optional)
        wait: Block until plan is ready (default: false)
        file_path: Path to a .mcp.json on the shared FS to inject into the plan job config (optional)

    Returns:
        JSON with plan_id, job_id, status, and (if wait=true) the plan content
    """
    try:
        from agenticore.runner import submit_plan_job

        job, plan = await submit_plan_job(task=task, repo_url=repo_url, wait=wait, file_path=file_path)
        return json.dumps(
            {
                "success": True,
                "plan_id": plan.id,
                "plan_name": plan.name,
                "job_id": job.id,
                "status": plan.status,
                "content": plan.content if wait else None,
            }
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_plan(plan_id: str) -> str:
    """Get a plan by ID, including its markdown content once ready.

    Args:
        plan_id: Plan UUID returned by plan_task

    Returns:
        JSON with plan details including status and content
    """
    try:
        from agenticore.plans import get_plan as _get_plan

        plan = _get_plan(plan_id)
        if plan is None:
            return json.dumps({"success": False, "error": f"Plan not found: {plan_id}"})

        return json.dumps({"success": True, "plan": plan.to_dict()})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_plans(limit: int = 20) -> str:
    """List recent plans with status and name.

    Args:
        limit: Max plans to return (default: 20)

    Returns:
        JSON with plans list
    """
    try:
        from agenticore.plans import list_plans as _list_plans

        plans = _list_plans(limit=limit)
        return json.dumps(
            {
                "success": True,
                "count": len(plans),
                "plans": [p.to_dict() for p in plans],
            }
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def execute_plan(
    plan_id: str,
    repo_url: str = "",
    profile: str = "",
    wait: bool = False,
    file_path: str = "",
) -> str:
    """Execute a ready plan by ID.

    Submits a normal coding job with the plan injected as context.

    Args:
        plan_id: Plan ID returned by plan_task
        repo_url: Override repo URL (defaults to the one used when planning)
        profile: Execution profile (default: coding)
        wait: Block until execution completes
        file_path: Path to a .mcp.json on the shared FS to inject into the execution job config (optional)

    Returns:
        JSON with job_id and status
    """
    try:
        from agenticore.runner import execute_plan_job

        job = await execute_plan_job(
            plan_id=plan_id, repo_url=repo_url, profile=profile, wait=wait, file_path=file_path
        )
        return json.dumps({"success": True, "job": job.to_dict()})
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _register_agent_mode_tools():
    """Register agent_completions MCP tool. Called only when AGENT_MODE=true."""

    @mcp.tool()
    async def agent_completions(
        message: str,
        uuid: str,
        wait: bool = True,
        stateless: bool = False,
        model: str = "",
        max_turns: int = 0,
        system_prompt: str = "",
        append_system_prompt: bool = True,
        permission_mode: str = "",
        output_format: str = "",
        effort: str = "",
        max_budget_usd: float = 0,
        fallback_model: str = "",
        allowed_tools: str = "",
        disallowed_tools: str = "",
        timeout: int = 0,
        context: str = "",
        meta: str = "{}",
        callback_url: str = "",
        notifications: str = "status",
        disable_mcp_servers: str = "",
    ) -> str:
        """Call the agent with a message. Returns JSON with result.

        Args:
            message: The task/prompt for the agent
            uuid: External correlation ID for session tracking
            wait: True=sync (block until done), False=async (queued, poll or callback)
            stateless: True=fresh session each call, False=resume conversation
            model: Model alias or full ID (default from config)
            max_turns: Agentic turn limit (default from config)
            system_prompt: Inline system prompt override (replaces system.md)
            append_system_prompt: True=append to defaults, False=replace defaults
            permission_mode: bypassPermissions, dontAsk, plan, etc.
            output_format: json, text, stream-json
            effort: low, medium, high
            max_budget_usd: Spending cap
            fallback_model: Auto-fallback on overload
            allowed_tools: Comma-separated tools that skip permission prompts
            disallowed_tools: Comma-separated tools removed entirely
            timeout: Max execution time in seconds
            context: JSON dict passed via stdin to Claude
            meta: Platform metadata JSON for hooks
            callback_url: Webhook URL for async notifications
            notifications: Comma-separated notification types (status,tool_call,thinking)

        Returns:
            JSON with agent response or acceptance message
        """
        try:
            ctx = None
            if context:
                try:
                    ctx = json.loads(context)
                except json.JSONDecodeError:
                    return json.dumps({"success": False, "error": "Invalid context JSON"})

            try:
                meta_dict = json.loads(meta) if meta else {}
            except json.JSONDecodeError:
                meta_dict = {}

            if wait:
                from agenticore.agent_mode.agent import AgentExecutor

                executor = AgentExecutor()
                # Parse disable_mcp_servers from comma-separated string to list
                dms = [s.strip() for s in disable_mcp_servers.split(",") if s.strip()] if disable_mcp_servers else None
                result = await executor.execute(
                    message=message,
                    external_uuid=uuid,
                    wait=wait,
                    stateless=stateless,
                    model=model,
                    max_turns=max_turns,
                    system_prompt=system_prompt,
                    append_system_prompt=append_system_prompt,
                    permission_mode=permission_mode,
                    output_format=output_format,
                    effort=effort,
                    max_budget_usd=max_budget_usd,
                    fallback_model=fallback_model,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
                    timeout=timeout,
                    context=ctx,
                    meta=meta_dict,
                    disable_mcp_servers=dms,
                )
                return json.dumps({"success": not result.get("is_error", False), **result})
            else:
                dms = [s.strip() for s in disable_mcp_servers.split(",") if s.strip()] if disable_mcp_servers else None
                return json.dumps(
                    _enqueue_async_completion(
                        message=message,
                        uuid=uuid,
                        callback_url=callback_url,
                        notifications_param=notifications,
                        request_params=dict(
                            stateless=stateless,
                            model=model,
                            max_turns=max_turns,
                            system_prompt=system_prompt,
                            append_system_prompt=append_system_prompt,
                            permission_mode=permission_mode,
                            output_format=output_format,
                            effort=effort,
                            max_budget_usd=max_budget_usd,
                            fallback_model=fallback_model,
                            allowed_tools=allowed_tools,
                            disallowed_tools=disallowed_tools,
                            timeout=timeout,
                            context=ctx,
                            meta=meta_dict,
                            disable_mcp_servers=dms,
                        ),
                    )
                )

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})


def _enqueue_async_completion(
    message: str,
    uuid: str,
    callback_url: str = "",
    notifications_param=None,
    request_params: dict = None,
) -> dict:
    """Create and enqueue an async completion. Returns response dict."""
    from agenticore.agent_mode.completions import create_completion, enqueue_completion
    from agenticore.agent_mode.notifications import (
        NotificationConfig,
        parse_notifications_param,
        save_notification_config,
    )

    completion = create_completion(
        uuid=uuid,
        message=message,
        callback_url=callback_url,
        request_params=request_params or {},
    )

    # Save notification config
    notif_types = parse_notifications_param(notifications_param)
    config = NotificationConfig(
        callback_url=callback_url,
        status=notif_types.get("status", True),
        tool_call=notif_types.get("tool_call", False),
        thinking=notif_types.get("thinking", False),
    )
    save_notification_config(uuid, config)

    # Enqueue — returns None if Redis available, dict if fallback needed, or {"rejected": True}
    cfg = get_config()
    max_depth = cfg.agent_mode.max_queue_workers * 2
    fallback = enqueue_completion(completion, max_queue_depth=max_depth)
    if fallback is not None:
        if isinstance(fallback, dict) and fallback.get("rejected"):
            return {"success": False, "error": "at capacity", "retry": True, **fallback}

        # No Redis — run inline as background task
        import asyncio

        from agenticore.agent_mode.worker import run_inline_completion

        asyncio.create_task(run_inline_completion(fallback))

    return {
        "success": True,
        "status": "queued",
        "uuid": uuid,
        "poll_url": f"/completions/{uuid}",
    }


@mcp.tool()
async def list_profiles() -> str:
    """List available execution profiles.

    Returns:
        JSON with profile names, descriptions, and key settings
    """
    try:
        from agenticore.profiles import load_profiles, profile_to_dict

        profiles = load_profiles()

        return json.dumps(
            {
                "success": True,
                "count": len(profiles),
                "profiles": [profile_to_dict(p) for p in profiles.values()],
            }
        )

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def prepare_worktree(repo_url: str, base_ref: str = "main") -> str:
    """Clone repo, create worktree, validate readiness. Returns worktree_id.

    Phase 1 of two-phase workflow. Use the returned worktree_id with run_task()
    to execute jobs in a known-good worktree.

    Args:
        repo_url: GitHub repo URL to clone
        base_ref: Base branch (default: main)

    Returns:
        JSON with worktree_id, path, branch, status
    """
    try:
        import asyncio

        from agenticore.repos import prepare_worktree as _prepare_worktree

        wt = await asyncio.to_thread(_prepare_worktree, repo_url=repo_url, base_ref=base_ref)
        return json.dumps({"success": True, "worktree": wt.to_dict()})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def get_worktree(worktree_id: str) -> str:
    """Get worktree details by ID.

    Args:
        worktree_id: Worktree UUID from prepare_worktree

    Returns:
        JSON with worktree details including status, path, branch
    """
    try:
        from agenticore.repos import get_worktree as _get_worktree

        wt = _get_worktree(worktree_id)
        if wt is None:
            return json.dumps({"success": False, "error": f"Worktree not found: {worktree_id}"})
        return json.dumps({"success": True, "worktree": wt.to_dict()})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def list_worktrees() -> str:
    """List all worktrees across cached repos with age, size, and push status.

    Returns:
        JSON with worktrees list (job_id, repo_key, branch, age_hours, size_bytes, pushed)
    """
    try:
        from agenticore.repos import list_all_worktrees, list_worktrees_from_store

        git_worktrees = list_all_worktrees()
        store_worktrees = list_worktrees_from_store()

        # Merge store metadata into git-level scan
        store_by_path = {wt.path: wt.to_dict() for wt in store_worktrees}
        for gwt in git_worktrees:
            meta = store_by_path.get(gwt["path"])
            if meta:
                gwt["worktree_id"] = meta.get("id", "")
                gwt["status"] = meta.get("status", "")

        return json.dumps({"success": True, "count": len(git_worktrees), "worktrees": git_worktrees})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def cleanup_worktrees(
    paths: str = "",
    max_age_hours: int = 0,
) -> str:
    """Remove worktrees by path or age threshold. Never called automatically.

    At least one of paths or max_age_hours must be provided.

    Args:
        paths: Comma-separated list of worktree paths to remove (from list_worktrees)
        max_age_hours: Remove all worktrees older than N hours (0 = disabled)

    Returns:
        JSON with removal results per worktree
    """
    try:
        from agenticore.repos import list_all_worktrees, remove_worktrees

        paths_to_remove = []

        if paths:
            paths_to_remove.extend([p.strip() for p in paths.split(",") if p.strip()])

        if max_age_hours > 0:
            all_wt = list_all_worktrees()
            for wt in all_wt:
                if wt["age_hours"] >= max_age_hours and wt["path"] not in paths_to_remove:
                    paths_to_remove.append(wt["path"])

        if not paths_to_remove:
            return json.dumps({"success": False, "error": "No worktrees to remove — provide paths or max_age_hours"})

        results = remove_worktrees(paths_to_remove)
        return json.dumps({"success": True, "removed": len([r for r in results if r["success"]]), "results": results})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ── REST API (via Starlette) ──────────────────────────────────────────────


def _build_rest_app():
    """Build a Starlette app with REST endpoints mirroring MCP tools."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    def health(request: Request):  # noqa: ARG001 — Starlette requires request param
        cfg = get_config()
        info = {"status": "ok", "service": "agenticore"}
        if cfg.agent_mode.enabled:
            info["agent_mode"] = True
            info["package_dir"] = cfg.agent_mode.package_dir
            info["default_model"] = cfg.agent_mode.model
        return JSONResponse(info)

    def _do_sync(target: str) -> dict:
        from agenticore.hooks import sync_agentihooks, sync_bundle, sync_agentihub, run_agentihooks_init, _bundle_dir

        cfg = get_config()
        results = {}

        if target in ("all", "bundle"):
            if cfg.agentihooks_bundle_url:
                try:
                    sync_bundle()
                    results["bundle"] = "ok"
                except Exception as e:
                    results["bundle"] = f"error: {e}"
            else:
                results["bundle"] = "skipped (no url)"

        if target in ("all", "agentihooks"):
            if cfg.agentihooks_url:
                try:
                    install_path = sync_agentihooks()
                    bundle_path = _bundle_dir() if cfg.agentihooks_bundle_url else None
                    pkg_dir = (
                        Path(cfg.agent_mode.package_dir) if cfg.agent_mode and cfg.agent_mode.package_dir else None
                    )
                    run_agentihooks_init(hooks_path=install_path, bundle_path=bundle_path, repo_dir=pkg_dir)
                    results["agentihooks"] = "ok"
                except Exception as e:
                    results["agentihooks"] = f"error: {e}"
            else:
                results["agentihooks"] = "skipped (no url)"

        if target in ("all", "agentihub"):
            if cfg.agentihub_url:
                try:
                    sync_agentihub()
                    results["agentihub"] = "ok"
                except Exception as e:
                    results["agentihub"] = f"error: {e}"
            else:
                results["agentihub"] = "skipped (no url)"

        return results

    async def post_admin_sync(request: Request):
        import asyncio

        target = request.query_params.get("target", "all")
        valid_targets = {"all", "agentihooks", "bundle", "agentihub"}
        if target not in valid_targets:
            return JSONResponse(
                {"error": f"Invalid target '{target}'. Must be one of: {', '.join(sorted(valid_targets))}"},
                status_code=400,
            )

        results = await asyncio.to_thread(_do_sync, target)
        return JSONResponse(results)

    async def post_jobs(request: Request):
        body = await request.json()
        result = await run_task(
            task=body.get("task", ""),
            repo_url=body.get("repo_url", ""),
            profile=body.get("profile", ""),
            base_ref=body.get("base_ref", "main"),
            session_id=body.get("session_id", ""),
            file_path=body.get("file_path", ""),
            create_repo=body.get("create_repo", False),
            private=body.get("private", True),
            worktree_id=body.get("worktree_id", ""),
            wait=body.get("wait", False),
        )
        data = json.loads(result)
        if data.get("retry"):
            status_code = 503
        elif data.get("success"):
            status_code = 200
        else:
            status_code = 400
        return JSONResponse(data, status_code=status_code)

    async def get_job_route(request: Request):
        job_id = request.path_params["job_id"]
        result = await get_job(job_id)
        data = json.loads(result)
        status_code = 200 if data.get("success") else 404
        return JSONResponse(data, status_code=status_code)

    async def get_jobs_route(request: Request):
        limit = int(request.query_params.get("limit", "20"))
        status = request.query_params.get("status", "")
        result = await list_jobs(limit=limit, status=status)
        return JSONResponse(json.loads(result))

    async def delete_job_route(request: Request):
        job_id = request.path_params["job_id"]
        result = await cancel_job(job_id)
        data = json.loads(result)
        status_code = 200 if data.get("success") else 404
        return JSONResponse(data, status_code=status_code)

    async def get_profiles_route(request: Request):
        result = await list_profiles()
        return JSONResponse(json.loads(result))

    async def post_plans(request: Request):
        body = await request.json()
        result = await plan_task(
            task=body.get("task", ""),
            repo_url=body.get("repo_url", ""),
            wait=body.get("wait", False),
            file_path=body.get("file_path", ""),
        )
        data = json.loads(result)
        status_code = 200 if data.get("success") else 400
        return JSONResponse(data, status_code=status_code)

    async def get_plans_route(request: Request):
        limit = int(request.query_params.get("limit", "20"))
        result = await list_plans(limit=limit)
        return JSONResponse(json.loads(result))

    async def get_plan_route(request: Request):
        plan_id = request.path_params["plan_id"]
        result = await get_plan(plan_id)
        data = json.loads(result)
        status_code = 200 if data.get("success") else 404
        return JSONResponse(data, status_code=status_code)

    async def post_execute_plan(request: Request):
        plan_id = request.path_params["plan_id"]
        body = await request.json()
        result = await execute_plan(
            plan_id=plan_id,
            repo_url=body.get("repo_url", ""),
            profile=body.get("profile", ""),
            wait=body.get("wait", False),
            file_path=body.get("file_path", ""),
        )
        data = json.loads(result)
        status_code = 200 if data.get("success") else 400
        return JSONResponse(data, status_code=status_code)

    async def post_worktrees(request: Request):
        body = await request.json()
        result = await prepare_worktree(
            repo_url=body.get("repo_url", ""),
            base_ref=body.get("base_ref", "main"),
        )
        data = json.loads(result)
        status_code = 200 if data.get("success") else 400
        return JSONResponse(data, status_code=status_code)

    async def get_worktrees_route(request: Request):
        result = await list_worktrees()
        return JSONResponse(json.loads(result))

    async def get_worktree_route(request: Request):
        worktree_id = request.path_params["worktree_id"]
        result = await get_worktree(worktree_id)
        data = json.loads(result)
        status_code = 200 if data.get("success") else 404
        return JSONResponse(data, status_code=status_code)

    async def delete_worktree_route(request: Request):
        worktree_id = request.path_params["worktree_id"]
        try:
            from agenticore.repos import get_worktree as _get_wt, remove_worktrees as _remove_wt

            wt = _get_wt(worktree_id)
            if wt is None:
                return JSONResponse({"success": False, "error": f"Worktree not found: {worktree_id}"}, status_code=404)
            results = _remove_wt([wt.path])
            return JSONResponse({"success": True, "results": results})
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    # Agent mode routes (conditional)
    cfg = get_config()
    if cfg.agent_mode.enabled:

        async def post_completions(request: Request):
            from agenticore.agent_mode import stream_config as sc

            body = await request.json()
            if "message" not in body or "uuid" not in body:
                return JSONResponse({"success": False, "error": "message and uuid are required"}, status_code=400)

            ctx = body.get("context")
            meta_dict = body.get("meta", {})
            wait = body.get("wait", True)

            agent_id = os.environ.get("AGENTIHUB_AGENT", "default")
            clean_message, _stream_cfg, _found = sc.get_for_request(agent_id, body["message"])
            body["message"] = clean_message

            if wait:
                from agenticore.agent_mode.agent import AgentExecutor
                from agenticore.runner import _get_job_semaphore

                sem = _get_job_semaphore()
                if sem.locked() and sem._value == 0:
                    return JSONResponse(
                        {"success": False, "error": "at capacity", "retry": True},
                        status_code=503,
                    )

                executor = AgentExecutor()
                result = await executor.execute(
                    message=body["message"],
                    external_uuid=body["uuid"],
                    wait=wait,
                    stateless=body.get("stateless", False),
                    model=body.get("model", ""),
                    max_turns=body.get("max_turns", 0),
                    system_prompt=body.get("system_prompt", ""),
                    append_system_prompt=body.get("append_system_prompt", True),
                    permission_mode=body.get("permission_mode", ""),
                    output_format=body.get("output_format", ""),
                    effort=body.get("effort", ""),
                    max_budget_usd=body.get("max_budget_usd", 0),
                    fallback_model=body.get("fallback_model", ""),
                    allowed_tools=body.get("allowed_tools", ""),
                    disallowed_tools=body.get("disallowed_tools", ""),
                    timeout=body.get("timeout", 0),
                    context=ctx,
                    meta=meta_dict,
                    disable_mcp_servers=body.get("disable_mcp_servers"),
                )
                is_error = result.get("is_error", False)
                return JSONResponse(
                    {"success": not is_error, **result},
                    status_code=200 if not is_error else 500,
                )
            else:
                result = _enqueue_async_completion(
                    message=body["message"],
                    uuid=body["uuid"],
                    callback_url=body.get("callback_url", ""),
                    notifications_param=body.get("notifications", "status"),
                    request_params=dict(
                        stateless=body.get("stateless", False),
                        model=body.get("model", ""),
                        max_turns=body.get("max_turns", 0),
                        system_prompt=body.get("system_prompt", ""),
                        append_system_prompt=body.get("append_system_prompt", True),
                        permission_mode=body.get("permission_mode", ""),
                        output_format=body.get("output_format", ""),
                        effort=body.get("effort", ""),
                        max_budget_usd=body.get("max_budget_usd", 0),
                        fallback_model=body.get("fallback_model", ""),
                        allowed_tools=body.get("allowed_tools", ""),
                        disallowed_tools=body.get("disallowed_tools", ""),
                        timeout=body.get("timeout", 0),
                        context=ctx,
                        meta=meta_dict,
                        disable_mcp_servers=body.get("disable_mcp_servers"),
                    ),
                )
                if not result.get("success", True) and result.get("retry"):
                    return JSONResponse(result, status_code=503)
                return JSONResponse(result, status_code=202)

        async def get_completions_list(request: Request):
            from agenticore.agent_mode.completions import list_completions

            limit = int(request.query_params.get("limit", "20"))
            status_filter = request.query_params.get("status", "")
            completions = list_completions(limit=limit, status=status_filter or None)
            return JSONResponse(
                {
                    "success": True,
                    "count": len(completions),
                    "completions": [c.to_dict() for c in completions],
                }
            )

        async def get_completion_route(request: Request):
            from agenticore.agent_mode.completions import get_completion

            uuid = request.path_params["uuid"]
            completion = get_completion(uuid)
            if completion is None:
                return JSONResponse(
                    {"success": False, "error": f"Completion not found: {uuid}"},
                    status_code=404,
                )
            return JSONResponse({"success": True, "completion": completion.to_dict()})

        async def patch_notifications_route(request: Request):
            from agenticore.agent_mode.notifications import update_notification_config

            uuid = request.path_params["uuid"]
            body = await request.json()
            config = update_notification_config(uuid, **body)
            if config is None:
                return JSONResponse(
                    {"success": False, "error": f"Notification config not found: {uuid}"},
                    status_code=404,
                )
            from dataclasses import asdict

            return JSONResponse({"success": True, "notifications": asdict(config)})

        async def post_openai_chat_completions(request: Request):
            from agenticore.agent_mode.agent import AgentExecutor
            from agenticore.agent_mode.openai_compat import (
                build_openai_error,
                build_openai_response,
                extract_request_id,
                flatten_messages,
                format_done,
                format_role_open_chunk,
                format_status_meta,
                format_stop_chunk,
            )
            from agenticore.agent_mode import stream_config as sc

            body = await request.json()
            messages = body.get("messages")
            if not messages:
                err, code = build_openai_error("messages is required", 400)
                return JSONResponse(err, status_code=code)

            stream = body.get("stream", False)
            raw_message = flatten_messages(messages)
            headers = {k.decode(): v.decode() for k, v in request.scope.get("headers", [])}
            request_uuid = extract_request_id(headers, body)
            raw_model = body.get("model", "")
            valid_models = {
                "sonnet",
                "opus",
                "haiku",
                "claude-sonnet-4-6",
                "claude-opus-4-6",
                "claude-haiku-4-5",
                "claude-sonnet-4-5",
                "claude-sonnet-4",
            }
            model_name = raw_model.split("/")[-1] if "/" in raw_model else raw_model
            if model_name not in valid_models:
                model_name = ""

            agent_id = os.environ.get("AGENTIHUB_AGENT", "default")
            clean_message, stream_cfg, found_tokens = sc.get_for_request(agent_id, raw_message)

            if "/stream-status" in found_tokens and not clean_message.strip():
                if stream:
                    async def status_gen():
                        yield format_role_open_chunk(model_name or "agenticore-agent", request_uuid)
                        yield format_status_meta(stream_cfg, model_name or "agenticore-agent", request_uuid)
                        yield format_stop_chunk({}, model_name or "agenticore-agent", request_uuid)
                        yield format_done()
                    return StreamingResponse(status_gen(), media_type="text/event-stream")
                return JSONResponse({"stream_config": stream_cfg, "agent_id": agent_id})

            from agenticore.runner import _get_job_semaphore

            sem = _get_job_semaphore()
            if sem.locked() and sem._value == 0:
                err, code = build_openai_error("at capacity", 503)
                return JSONResponse(err, status_code=code)

            executor = AgentExecutor()

            if stream:
                gen = executor.execute_streaming(
                    message=clean_message,
                    external_uuid=request_uuid,
                    stream_cfg=stream_cfg,
                    model=model_name,
                    timeout=body.get("timeout", 0),
                    disable_mcp_servers=body.get("disable_mcp_servers"),
                    request_uuid=request_uuid,
                    sse_model_name=model_name or "agenticore-agent",
                )
                return StreamingResponse(gen, media_type="text/event-stream")

            async with sem:
                result = await executor.execute(
                    message=clean_message,
                    external_uuid=request_uuid,
                    wait=True,
                    stateless=True,
                    model=model_name,
                    timeout=body.get("timeout", 0),
                    disable_mcp_servers=body.get("disable_mcp_servers"),
                )

            if result.get("is_error"):
                err, code = build_openai_error(result.get("error", "Agent error"), 500)
                return JSONResponse(err, status_code=code)

            return JSONResponse(build_openai_response(result, model_name, request_uuid))

        async def get_openai_models(request: Request):
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": cfg.agent_mode.model or "agenticore-agent",
                            "object": "model",
                            "created": 0,
                            "owned_by": "agenticore",
                        }
                    ],
                }
            )

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/jobs", post_jobs, methods=["POST"]),
        Route("/jobs", get_jobs_route, methods=["GET"]),
        Route("/jobs/{job_id}", get_job_route, methods=["GET"]),
        Route("/jobs/{job_id}", delete_job_route, methods=["DELETE"]),
        Route("/profiles", get_profiles_route, methods=["GET"]),
        Route("/plans", post_plans, methods=["POST"]),
        Route("/plans", get_plans_route, methods=["GET"]),
        Route("/plans/{plan_id}", get_plan_route, methods=["GET"]),
        Route("/plans/{plan_id}/execute", post_execute_plan, methods=["POST"]),
        Route("/worktrees", post_worktrees, methods=["POST"]),
        Route("/worktrees", get_worktrees_route, methods=["GET"]),
        Route("/worktrees/{worktree_id}", get_worktree_route, methods=["GET"]),
        Route("/worktrees/{worktree_id}", delete_worktree_route, methods=["DELETE"]),
        Route("/admin/sync", post_admin_sync, methods=["POST"]),
    ]

    if cfg.agent_mode.enabled:
        routes.extend(
            [
                Route("/completions", post_completions, methods=["POST"]),
                Route("/completions", get_completions_list, methods=["GET"]),
                Route("/completions/{uuid:path}", get_completion_route, methods=["GET"]),
                Route("/completions/{uuid:path}/notifications", patch_notifications_route, methods=["PATCH"]),
                Route("/v1/chat/completions", post_openai_chat_completions, methods=["POST"]),
                Route("/v1/models", get_openai_models, methods=["GET"]),
            ]
        )

    return Starlette(routes=routes)


# ── Transport ─────────────────────────────────────────────────────────────


async def _handle_lifespan(_scope, receive, send, lifespan_factory):
    """Handle ASGI lifespan events (startup/shutdown)."""
    lifespan_cm = None
    message = await receive()
    if message["type"] == "lifespan.startup":
        try:
            lifespan_cm = lifespan_factory()
            await lifespan_cm.__aenter__()
            await send({"type": "lifespan.startup.complete"})
        except Exception:
            await send({"type": "lifespan.startup.failed"})
            return
    message = await receive()
    if message["type"] == "lifespan.shutdown":
        if lifespan_cm:
            await lifespan_cm.__aexit__(None, None, None)
        await send({"type": "lifespan.shutdown.complete"})


async def _route_request(scope, receive, send, http_app, sse_app, rest_app):
    """Route an HTTP/WebSocket request to the appropriate sub-app."""
    path = scope.get("path", "")
    if path.startswith("/mcp"):
        await http_app(scope, receive, send)
    elif path in ("/sse", "/messages") or path.startswith("/messages"):
        await sse_app(scope, receive, send)
    else:
        await rest_app(scope, receive, send)


def _build_asgi_app():
    """Build the full ASGI app: MCP + REST + auth + health."""
    from contextlib import asynccontextmanager

    rest_app = _build_rest_app()
    http_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    session_manager = mcp.session_manager

    @asynccontextmanager
    async def lifespan():
        async with session_manager.run():
            yield

    async def combined_app(scope, receive, send):
        if scope["type"] == "lifespan":
            await _handle_lifespan(scope, receive, send, lifespan)
            return
        await _route_request(scope, receive, send, http_app, sse_app, rest_app)

    cfg = get_config()
    if cfg.server.api_keys:
        return _ApiKeyMiddleware(combined_app, cfg.server.api_keys)
    return combined_app


class _ApiKeyMiddleware:
    """Simple API key auth middleware."""

    _PUBLIC_PATHS = frozenset({"/health"})

    def __init__(self, app, api_keys):
        self.app = app
        self.api_keys = set(api_keys)

    def _extract_api_key(self, scope) -> str:
        """Extract API key from headers or query string.

        Accepted forms:
          1. X-API-Key: <key>             (MCP client JSON config, preferred)
          2. Authorization: Bearer <key>  (OAuth Bearer / MCP type:http clients)
          3. ?api_key=<key>               (REST API / curl convenience)
        """
        for name, value in scope.get("headers", []):
            n = name.lower()
            if n == b"x-api-key":
                return value.decode("utf-8")
            if n == b"authorization":
                v = value.decode("utf-8")
                return v[7:] if v.startswith("Bearer ") else ""
        qs = scope.get("query_string", b"").decode("utf-8")
        for param in qs.split("&"):
            if param.startswith("api_key="):
                return param[8:]
        return ""

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        key = self._extract_api_key(scope)
        if key not in self.api_keys:
            body = json.dumps({"error": "Invalid or missing API key"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def run_sse_server() -> None:
    """Start the HTTP server (SSE + REST + MCP)."""
    try:
        import uvicorn
    except ImportError:
        print("SSE transport requires uvicorn: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    cfg = get_config()
    app = _build_asgi_app()

    print(f"Agenticore server on {cfg.server.host}:{cfg.server.port}", file=sys.stderr)
    print(f"  MCP:    http://{cfg.server.host}:{cfg.server.port}/mcp", file=sys.stderr)
    print(f"  REST:   http://{cfg.server.host}:{cfg.server.port}/jobs", file=sys.stderr)
    print(f"  Health: http://{cfg.server.host}:{cfg.server.port}/health", file=sys.stderr)

    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


def _do_sync_agentihooks(cfg) -> Optional[Path]:
    """Clone/fetch agentihooks repo. Thread-safe. Returns install path."""
    if cfg.dev_mode or (not cfg.agentihooks_url and not os.getenv("AGENTICORE_AGENTIHOOKS_PATH")):
        from agenticore.hooks import sync_agentihooks

        return sync_agentihooks()
    if not cfg.agentihooks_url:
        return None
    from agenticore.hooks import sync_agentihooks

    return sync_agentihooks()


def _do_sync_bundle(cfg) -> Optional[Path]:
    """Clone/fetch agentihooks-bundle repo. Thread-safe. Returns bundle path."""
    from agenticore.hooks import sync_bundle

    return sync_bundle()


def _do_sync_agentihub(cfg) -> Optional[Path]:
    """Clone/fetch agentihub repo. Thread-safe. Returns hub path."""
    if cfg.dev_mode:
        from agenticore.hooks import sync_agentihub

        return sync_agentihub()
    if not cfg.agentihub_url:
        return None
    from agenticore.hooks import sync_agentihub

    return sync_agentihub()


def _finish_agentihooks_init(cfg, hooks_path: Optional[Path], bundle_path: Optional[Path]) -> None:
    """Run agentihooks init + start watchers. Must run after git syncs complete."""
    from agenticore.hooks import run_agentihooks_init

    if hooks_path or bundle_path:
        pkg_dir = Path(cfg.agent_mode.package_dir) if cfg.agent_mode and cfg.agent_mode.package_dir else None
        run_agentihooks_init(hooks_path=hooks_path, bundle_path=bundle_path, repo_dir=pkg_dir)

    if cfg.dev_mode:
        logger.info("dev mode: agentihooks init complete (no watchers)")
        return

    # Start watchers (daemon threads — safe to start from main thread)
    if hooks_path and cfg.agentihooks_url and cfg.agentihooks_sync_interval > 0:
        from agenticore.hooks import start_sync_watcher

        start_sync_watcher(cfg.agentihooks_url, hooks_path, cfg.agentihooks_sync_interval)
    if bundle_path and cfg.agentihooks_bundle_url and cfg.agentihooks_bundle_sync_interval > 0:
        from agenticore.hooks import start_bundle_watcher

        start_bundle_watcher(cfg.agentihooks_bundle_url, bundle_path, cfg.agentihooks_bundle_sync_interval)


def _start_agentihub_watcher_if_needed(cfg, hub_path: Optional[Path]) -> None:
    """Start agentihub watcher daemon thread if configured."""
    if cfg.dev_mode or not hub_path or not cfg.agentihub_url:
        return
    if cfg.agentihub_sync_interval > 0:
        from agenticore.hooks import start_agentihub_watcher

        start_agentihub_watcher(cfg.agentihub_url, hub_path, cfg.agentihub_sync_interval)


def _auto_register_with_bridge(cfg):
    """Register with AgentiBridge for A2A discovery. Best-effort — never blocks startup."""
    if not cfg.agentibridge.url or not cfg.agentibridge.registration_enabled:
        return
    try:
        from agenticore.bridge_client import register_with_bridge, start_heartbeat_thread
        from agenticore.profiles import load_profiles

        profiles = load_profiles()
        agent_id = register_with_bridge(cfg, profiles)
        if agent_id and cfg.agentibridge.heartbeat_interval > 0:
            start_heartbeat_thread(cfg, agent_id, profiles)
    except Exception as e:
        logger.warning("AgentiBridge registration failed: %s — A2A discovery unavailable", e)


def _safe_result(future: Future, label: str):
    """Extract result from a future, logging failures as non-fatal."""
    try:
        return future.result()
    except Exception as e:
        logger.warning("%s failed (non-fatal): %s", label, e)
        return None


def _install_bashrc() -> None:
    """Install agenticore bashrc snippet into ~/.bashrc. Non-fatal."""
    bashrc_src = Path("/opt/agenticore/bashrc")
    bashrc_dst = Path.home() / ".bashrc"
    if bashrc_src.exists():
        try:
            content = bashrc_dst.read_text() if bashrc_dst.exists() else ""
            marker = "# agenticore-shell"
            if marker in content:
                content = content[: content.index(marker)]
            content += f"{marker}\n{bashrc_src.read_text()}"
            bashrc_dst.write_text(content)
        except OSError:
            pass


def _bun_install_plugin_deps(runtime: Path) -> None:
    """Install missing node_modules for plugins. Non-fatal, runs in background."""
    try:
        import glob as _glob

        for pkg_json in _glob.glob(str(runtime / "plugins" / "cache" / "*" / "*" / "*" / "package.json")):
            pkg_dir = Path(pkg_json).parent
            if not (pkg_dir / "node_modules").exists():
                subprocess.run(
                    ["bun", "install", "--no-summary"],
                    cwd=str(pkg_dir),
                    capture_output=True,
                    timeout=60,
                )
                logger.info("installed plugin deps in %s", pkg_dir)
    except Exception as e:
        logger.warning("bun install plugin deps failed (non-fatal): %s", e)


def _ensure_claude_onboarding():
    """Ensure .claude.json has required flags for non-interactive operation.

    Sets hasCompletedOnboarding (skips login prompt), hasTrustDialogAccepted
    at root level, and project-level trust for the agent package directory
    (required for interactive/channel modes like Telegram).

    Plugin dep install (bun) is NOT done here — it runs as a background thread.
    """
    claude_json = Path.home() / ".claude.json"
    try:
        data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
        dirty = False
        if not data.get("hasCompletedOnboarding"):
            data["hasCompletedOnboarding"] = True
            data["installMethod"] = "native"
            dirty = True
        if not data.get("hasTrustDialogAccepted"):
            data["hasTrustDialogAccepted"] = True
            dirty = True

        # Project-level trust for agent package dir (channel mode requires this)
        agent_name = os.environ.get("AGENTIHUB_AGENT", "").strip()
        if agent_name:
            home = Path.home()
            pkg_path = str(home / "agentihub" / "agents" / agent_name / "package")
            projects = data.setdefault("projects", {})
            if pkg_path not in projects or not projects[pkg_path].get("hasTrustDialogAccepted"):
                projects[pkg_path] = {"hasTrustDialogAccepted": True}
                dirty = True

        if dirty:
            claude_json.write_text(json.dumps(data, indent=2))
            logger.info(
                "claude onboarding: onboarding=%s trust=%s agent=%s",
                data["hasCompletedOnboarding"],
                data["hasTrustDialogAccepted"],
                agent_name or "none",
            )
    except Exception as e:
        logger.warning("claude onboarding patch failed: %s — non-fatal", e)

    # Migrate baked-in plugins/marketplace from image to runtime HOME
    try:
        baked = Path("/home/agenticore/.claude")
        runtime = Path.home() / ".claude"
        if baked.exists() and baked.resolve() != runtime.resolve():
            for subdir in ("plugins", "marketplace"):
                src = baked / subdir
                dst = runtime / subdir
                if src.exists() and not dst.exists():
                    shutil.copytree(str(src), str(dst))
                    logger.info("migrated baked %s → %s", src, dst)
            # Fix installPath in installed_plugins.json (baked as /root/.claude/...)
            ip_file = runtime / "plugins" / "installed_plugins.json"
            if ip_file.exists():
                ip_data = json.loads(ip_file.read_text())
                dirty = False
                for entries in ip_data.get("plugins", {}).values():
                    for entry in entries:
                        old_path = entry.get("installPath", "")
                        if "/root/.claude/" in old_path:
                            entry["installPath"] = old_path.replace("/root/.claude/", str(runtime) + "/")
                            dirty = True
                if dirty:
                    ip_file.write_text(json.dumps(ip_data, indent=2))
                    logger.info("fixed installPath in installed_plugins.json")
            # Fix marketplace installLocation paths too
            km_file = runtime / "plugins" / "known_marketplaces.json"
            if km_file.exists():
                km_data = json.loads(km_file.read_text())
                dirty = False
                for mp in km_data.values():
                    loc = mp.get("installLocation", "")
                    if "/root/.claude/" in loc:
                        mp["installLocation"] = loc.replace("/root/.claude/", str(runtime) + "/")
                        dirty = True
                if dirty:
                    km_file.write_text(json.dumps(km_data, indent=2))
                    logger.info("fixed installLocation in known_marketplaces.json")
    except Exception as e:
        logger.warning("plugin migration failed: %s — non-fatal", e)


def _enable_installed_plugins():
    """Inject enabledPlugins into settings.json for all installed plugins.

    Must run AFTER agentihooks init, which regenerates settings.json
    and doesn't know about Claude Code plugins.
    """
    try:
        runtime = Path.home() / ".claude"
        settings_path = runtime / "settings.json"
        plugins_json = runtime / "plugins" / "installed_plugins.json"
        if not settings_path.exists() or not plugins_json.exists():
            return
        settings = json.loads(settings_path.read_text())
        installed = json.loads(plugins_json.read_text())
        plugin_names = list(installed.get("plugins", {}).keys())
        if not plugin_names:
            return
        enabled = settings.get("enabledPlugins", {})
        changed = False
        for name in plugin_names:
            if name not in enabled:
                enabled[name] = True
                changed = True
        if changed:
            settings["enabledPlugins"] = enabled
            settings_path.write_text(json.dumps(settings, indent=2))
            logger.info("enabled plugins in settings.json: %s", plugin_names)
    except Exception as e:
        logger.warning("plugin enable failed: %s — non-fatal", e)


def main():
    """Entrypoint for ``python -m agenticore``.

    Phased parallel startup:
      Phase 1 — setup tasks + git clones (all parallel via ThreadPoolExecutor)
      Phase 2 — agentihooks init + enable plugins (sequential, needs Phase 1)
      Phase 3 — agent mode init (sequential, needs Phase 2)
    """
    t0 = time.monotonic()

    # Auto-reap orphaned child processes (zombie prevention when running as PID 1)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    cfg = get_config()

    print("Starting Agenticore...", file=sys.stderr)

    # ── Phase 1: fire all independent tasks in parallel ──────────────
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="boot") as ex:
        # Background bun install — fire and forget, never blocks boot
        runtime_claude = Path.home() / ".claude"
        ex.submit(_bun_install_plugin_deps, runtime_claude)

        # Setup tasks (instant I/O)
        f_git1 = ex.submit(
            subprocess.run,
            ["git", "config", "--global", "--unset-all", "credential.https://github.com.helper"],
            capture_output=True,
        )
        f_git2 = ex.submit(
            subprocess.run,
            ["git", "config", "--global", "--unset-all", "credential.https://gist.github.com.helper"],
            capture_output=True,
        )
        f_bashrc = ex.submit(_install_bashrc)
        f_onboard = ex.submit(_ensure_claude_onboarding)

        # Git clones — all 3 in parallel
        f_hooks = ex.submit(_do_sync_agentihooks, cfg)
        f_bundle = ex.submit(_do_sync_bundle, cfg)
        f_hub = ex.submit(_do_sync_agentihub, cfg)

        # Bridge registration — best-effort, overlaps with everything
        f_bridge = ex.submit(_auto_register_with_bridge, cfg)

        # Wait for setup tasks (fast, <1s)
        for f in [f_git1, f_git2, f_bashrc, f_onboard]:
            _safe_result(f, "setup")

        t1 = time.monotonic()

        # Wait for all 3 git clones
        hooks_path = _safe_result(f_hooks, "sync_agentihooks")
        bundle_path = _safe_result(f_bundle, "sync_bundle")
        hub_path = _safe_result(f_hub, "sync_agentihub")

        t2 = time.monotonic()
        logger.info("boot phase1 done in %.2fs (setup=%.2fs git=%.2fs)", t2 - t0, t1 - t0, t2 - t1)

    # ── Phase 2: agentihooks init + plugins (sequential) ─────────────
    try:
        _finish_agentihooks_init(cfg, hooks_path, bundle_path)
    except Exception as e:
        logger.warning("agentihooks init failed: %s — profiles may be unavailable", e)

    _enable_installed_plugins()  # must run AFTER agentihooks regenerates settings.json
    _start_agentihub_watcher_if_needed(cfg, hub_path)

    # Bridge future — don't care about result, already submitted
    _safe_result(f_bridge, "bridge_registration")

    t3 = time.monotonic()
    logger.info("boot phase2 done in %.2fs (init+plugins)", t3 - t2)

    # ── Phase 3: agent mode (needs hub_path + agentihooks init) ──────
    if cfg.agent_mode.enabled:
        print("Agent mode enabled — initializing...", file=sys.stderr)
        _register_agent_mode_tools()
        from agenticore.agent_mode.initializer import initialize_agent_mode

        initialize_agent_mode()

    t4 = time.monotonic()

    tools = mcp._tool_manager.list_tools()
    print(f"Tools: {len(tools)}", file=sys.stderr)
    for t in tools:
        print(f"  - {t.name}", file=sys.stderr)

    print(
        f"Agenticore ready in {t4 - t0:.1f}s "
        f"(setup={t1 - t0:.1f}s git={t2 - t1:.1f}s init={t3 - t2:.1f}s agent={t4 - t3:.1f}s)",
        file=sys.stderr,
    )

    if cfg.server.transport == "sse":
        run_sse_server()
    else:
        mcp.run()
