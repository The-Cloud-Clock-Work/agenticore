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
import sys

from mcp.server.fastmcp import FastMCP

from agenticore.config import get_config

logger = logging.getLogger(__name__)


def _build_oauth_config():
    """Build OAuth provider + AuthSettings if OAUTH_ISSUER_URL is set."""
    import os

    issuer = os.getenv("OAUTH_ISSUER_URL")
    if not issuer:
        return None, None

    from agenticore.oauth_provider import AgenticoreOAuthProvider

    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    except ImportError:
        print("WARNING: mcp package does not support OAuth (upgrade to >=1.26)", file=sys.stderr)
        return None, None

    provider = AgenticoreOAuthProvider(
        issuer_url=issuer,
        client_id=os.getenv("OAUTH_CLIENT_ID", ""),
        client_secret=os.getenv("OAUTH_CLIENT_SECRET", ""),
    )

    allowed_scopes_raw = os.getenv("OAUTH_ALLOWED_SCOPES", "").strip()
    allowed_scopes = [s.strip() for s in allowed_scopes_raw.split() if s.strip()] if allowed_scopes_raw else None

    resource_url = os.getenv("OAUTH_RESOURCE_URL") or (issuer.rstrip("/") + "/mcp")

    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=allowed_scopes,
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    return provider, settings


def _make_mcp() -> FastMCP:
    cfg = get_config()
    oauth_provider, oauth_settings = _build_oauth_config()
    kwargs = {
        "host": cfg.server.host,
        "port": cfg.server.port,
        "json_response": True,
    }
    if oauth_provider and oauth_settings:
        kwargs["auth_server_provider"] = oauth_provider
        kwargs["auth"] = oauth_settings
        print("OAuth 2.1 enabled (OAUTH_ISSUER_URL is set)", file=sys.stderr)
    return FastMCP("agenticore", **kwargs)


mcp = _make_mcp()


# ── MCP Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def run_task(
    task: str,
    repo_url: str = "",
    profile: str = "",
    base_ref: str = "main",
    wait: bool = False,
    session_id: str = "",
    file_path: str = "",
) -> str:
    """Submit a task for Claude Code execution.

    Async (fire and forget) by default — returns job ID immediately.
    Pass wait=true to block until completion.

    Args:
        task: What Claude should do
        repo_url: GitHub repo URL to clone (optional — omit for local tasks)
        profile: Execution profile name (default: auto-routed)
        base_ref: Base branch (default: main)
        wait: Block until completion (default: false)
        session_id: Claude session ID to resume (optional)
        file_path: Path to a .mcp.json on the shared FS to inject into the job config (optional)

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
        )

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
                )
                return json.dumps({"success": not result.get("is_error", False), **result})
            else:
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

    # Enqueue — returns None if Redis available, dict if fallback needed
    fallback = enqueue_completion(completion)
    if fallback is not None:
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


# ── REST API (via Starlette) ──────────────────────────────────────────────


def _build_rest_app():
    """Build a Starlette app with REST endpoints mirroring MCP tools."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def health(request: Request):  # noqa: ARG001 — Starlette requires request param
        cfg = get_config()
        info = {"status": "ok", "service": "agenticore"}
        if cfg.agent_mode.enabled:
            info["agent_mode"] = True
            info["package_dir"] = cfg.agent_mode.package_dir
            info["default_model"] = cfg.agent_mode.model
        return JSONResponse(info)

    async def post_jobs(request: Request):
        body = await request.json()
        result = await run_task(
            task=body.get("task", ""),
            repo_url=body.get("repo_url", ""),
            profile=body.get("profile", ""),
            base_ref=body.get("base_ref", "main"),
            wait=body.get("wait", False),
            session_id=body.get("session_id", ""),
            file_path=body.get("file_path", ""),
        )
        data = json.loads(result)
        status_code = 200 if data.get("success") else 400
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

    # Agent mode routes (conditional)
    cfg = get_config()
    if cfg.agent_mode.enabled:

        async def post_completions(request: Request):
            body = await request.json()
            if "message" not in body or "uuid" not in body:
                return JSONResponse({"success": False, "error": "message and uuid are required"}, status_code=400)

            ctx = body.get("context")
            meta_dict = body.get("meta", {})
            wait = body.get("wait", True)

            if wait:
                from agenticore.agent_mode.agent import AgentExecutor

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
                    ),
                )
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
    ]

    if cfg.agent_mode.enabled:
        routes.extend(
            [
                Route("/completions", post_completions, methods=["POST"]),
                Route("/completions", get_completions_list, methods=["GET"]),
                Route("/completions/{uuid:path}", get_completion_route, methods=["GET"]),
                Route("/completions/{uuid:path}/notifications", patch_notifications_route, methods=["PATCH"]),
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


def _auto_sync_agentihooks(cfg):
    """Auto-sync agentihooks if URL is configured and path not already set."""
    if not cfg.agentihooks_url or os.getenv("AGENTICORE_AGENTIHOOKS_PATH"):
        return
    try:
        from agenticore.hooks import sync_agentihooks, start_sync_watcher

        install_path = sync_agentihooks()
        if install_path and cfg.agentihooks_sync_interval > 0:
            start_sync_watcher(cfg.agentihooks_url, install_path, cfg.agentihooks_sync_interval)
    except Exception as e:
        logger.warning("agentihooks sync failed: %s — profiles may be unavailable", e)


def _auto_sync_agentihub(cfg):
    """Auto-sync agentihub if URL is configured (clone only, no profile build).

    Agent mode handles its own provisioning via initializer.py.
    This just ensures the repo is available on shared FS.
    """
    if not cfg.agentihub_url:
        return
    try:
        from agenticore.hooks import sync_agentihub

        sync_agentihub()
    except Exception as e:
        logger.warning("agentihub sync failed: %s — non-fatal", e)


def _auto_sync_mcp_lib(cfg):
    """Auto-sync MCP lib if URL is configured and path not already set."""
    if not cfg.mcp_lib_url or os.getenv("AGENTICORE_MCP_LIB_PATH"):
        return
    try:
        from agenticore.hooks import sync_mcp_lib, start_mcp_lib_watcher

        mcp_lib_path = sync_mcp_lib()
        if mcp_lib_path and cfg.agentihooks_sync_interval > 0:
            start_mcp_lib_watcher(cfg.mcp_lib_url, mcp_lib_path, cfg.agentihooks_sync_interval)
    except Exception as e:
        logger.warning("mcp-lib sync failed: %s — extra MCPs unavailable", e)


def main():
    """Entrypoint for ``python -m agenticore``."""
    cfg = get_config()

    print("Starting Agenticore...", file=sys.stderr)

    _auto_sync_agentihooks(cfg)
    _auto_sync_agentihub(cfg)
    _auto_sync_mcp_lib(cfg)

    # Agent mode initialization
    if cfg.agent_mode.enabled:
        print("Agent mode enabled — initializing...", file=sys.stderr)
        _register_agent_mode_tools()
        from agenticore.agent_mode.initializer import initialize_agent_mode

        initialize_agent_mode()

    tools = mcp._tool_manager.list_tools()
    print(f"Tools: {len(tools)}", file=sys.stderr)
    for t in tools:
        print(f"  - {t.name}", file=sys.stderr)

    if cfg.server.transport == "sse":
        run_sse_server()
    else:
        mcp.run()
