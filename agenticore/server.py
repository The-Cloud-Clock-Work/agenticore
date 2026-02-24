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
import sys

from mcp.server.fastmcp import FastMCP

from agenticore.config import get_config


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
    wait: bool = False,
    session_id: str = "",
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
        return JSONResponse({"status": "ok", "service": "agenticore"})

    async def post_jobs(request: Request):
        body = await request.json()
        result = await run_task(
            task=body.get("task", ""),
            repo_url=body.get("repo_url", ""),
            profile=body.get("profile", ""),
            base_ref=body.get("base_ref", "main"),
            wait=body.get("wait", False),
            session_id=body.get("session_id", ""),
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

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/jobs", post_jobs, methods=["POST"]),
        Route("/jobs", get_jobs_route, methods=["GET"]),
        Route("/jobs/{job_id}", get_job_route, methods=["GET"]),
        Route("/jobs/{job_id}", delete_job_route, methods=["DELETE"]),
        Route("/profiles", get_profiles_route, methods=["GET"]),
    ]

    return Starlette(routes=routes)


# ── Transport ─────────────────────────────────────────────────────────────


async def _handle_lifespan(scope, receive, send, lifespan_factory):
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

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract API key
        key = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-api-key":
                key = value.decode("utf-8")
                break

        if key is None:
            qs = scope.get("query_string", b"").decode("utf-8")
            for param in qs.split("&"):
                if param.startswith("api_key="):
                    key = param[8:]
                    break

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


def main():
    """Entrypoint for ``python -m agenticore``."""
    cfg = get_config()

    print("Starting Agenticore...", file=sys.stderr)

    tools = mcp._tool_manager.list_tools()
    print(f"Tools: {len(tools)}", file=sys.stderr)
    for t in tools:
        print(f"  - {t.name}", file=sys.stderr)

    if cfg.server.transport == "sse":
        run_sse_server()
    else:
        mcp.run()
