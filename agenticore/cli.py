"""CLI entrypoint for Agenticore.

Usage::

    agenticore run "fix the auth bug" --repo https://github.com/org/repo
    agenticore run "add tests" --wait       Submit and wait for completion
    agenticore serve                        Start the server
    agenticore jobs                         List recent jobs
    agenticore job <job_id>                 Get job details
    agenticore cancel <job_id>              Cancel a job
    agenticore profiles                     List profiles
    agenticore status                       Check server health
    agenticore update                       Update agenticore to latest version
    agenticore version                      Show version
    agenticore init-shared-fs               Initialise shared FS layout (Kubernetes)
    agenticore drain                        Drain pod before shutdown (Kubernetes)
    agenticore hooks sync [--url URL]       Clone/update agentihooks repo
"""

import argparse
import json
import sys

from agenticore import __version__


def _api_url():
    import os

    host = os.getenv("AGENTICORE_HOST", "127.0.0.1")
    port = os.getenv("AGENTICORE_PORT", "8200")
    return f"http://{host}:{port}"


def _api_get(path: str) -> dict:
    import httpx

    resp = httpx.get(f"{_api_url()}{path}", timeout=10)
    return resp.json()


def _api_post(path: str, data: dict) -> dict:
    import httpx

    resp = httpx.post(f"{_api_url()}{path}", json=data, timeout=30)
    return resp.json()


def _api_delete(path: str) -> dict:
    import httpx

    resp = httpx.delete(f"{_api_url()}{path}", timeout=10)
    return resp.json()


def _print_json(data: dict):
    print(json.dumps(data, indent=2))


def _cmd_serve(args):
    """Start the server."""
    import os

    if args.port:
        os.environ["AGENTICORE_PORT"] = str(args.port)
    if args.host:
        os.environ["AGENTICORE_HOST"] = args.host

    from agenticore.server import main

    main()


def _cmd_run(args):
    """Submit a task."""
    payload = {
        "task": args.task,
        "repo_url": args.repo or "",
        "profile": args.profile or "",
        "base_ref": args.base_ref,
        "wait": args.wait,
    }
    if args.session_id:
        payload["session_id"] = args.session_id
    if args.file_path:
        payload["file_path"] = args.file_path

    try:
        data = _api_post("/jobs", payload)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Is the server running? Try: agenticore run", file=sys.stderr)
        sys.exit(1)

    if data.get("success"):
        job = data["job"]
        print(f"Job submitted: {job['id']}")
        print(f"  Status:  {job['status']}")
        print(f"  Profile: {job.get('profile', 'coding')}")
        if job.get("repo_url"):
            print(f"  Repo:    {job['repo_url']}")
        if args.wait and job.get("output"):
            print(f"\n{job['output']}")
    else:
        print(f"Error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


def _cmd_jobs(args):
    """List recent jobs."""
    try:
        params = f"?limit={args.limit}"
        if args.status:
            params += f"&status={args.status}"
        data = _api_get(f"/jobs{params}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not data.get("success"):
        print(f"Error: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    jobs = data.get("jobs", [])
    if not jobs:
        print("No jobs found.")
        return

    # Table output
    print(f"{'ID':<38} {'STATUS':<12} {'PROFILE':<10} {'TASK'}")
    print("-" * 90)
    for j in jobs:
        task_short = j.get("task", "")[:40]
        print(f"{j['id']:<38} {j['status']:<12} {j.get('profile', ''):<10} {task_short}")


def _print_job_details(job: dict) -> None:
    """Print human-readable job details."""
    print(f"Job:     {job['id']}")
    print(f"Status:  {job['status']}")
    print(f"Profile: {job.get('profile', '')}")
    print(f"Task:    {job.get('task', '')}")

    _OPTIONAL_FIELDS = [
        ("repo_url", "Repo:    "),
        ("exit_code", "Exit:    "),
        ("pr_url", "PR:      "),
        ("created_at", "Created: "),
        ("ended_at", "Ended:   "),
    ]
    for key, label in _OPTIONAL_FIELDS:
        val = job.get(key)
        if val is not None:
            print(f"{label}{val}")

    if job.get("error"):
        print(f"\nError:\n{job['error']}")
    if job.get("output"):
        print(f"\nOutput:\n{job['output'][:2000]}")


def _cmd_job(args):
    """Get job details."""
    try:
        data = _api_get(f"/jobs/{args.job_id}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not data.get("success"):
        print(f"Error: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        _print_json(data["job"])
    else:
        _print_job_details(data["job"])


def _cmd_cancel(args):
    """Cancel a job."""
    try:
        data = _api_delete(f"/jobs/{args.job_id}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if data.get("success"):
        print(f"Job {args.job_id}: {data['job']['status']}")
    else:
        print(f"Error: {data.get('error')}", file=sys.stderr)
        sys.exit(1)


def _cmd_profiles(args):
    """List profiles."""
    try:
        data = _api_get("/profiles")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not data.get("success"):
        print(f"Error: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    profiles = data.get("profiles", [])
    if not profiles:
        print("No profiles found.")
        return

    for p in profiles:
        print(f"  {p['name']:<12} {p.get('description', '')}")
        print(
            f"               model={p.get('model', '')} max_turns={p.get('max_turns', '')} auto_pr={p.get('auto_pr', '')}"
        )


def _cmd_status(args):
    """Check server health."""
    try:
        data = _api_get("/health")
        print(f"Status:  {data.get('status', 'unknown')}")
        print(f"Service: {data.get('service', 'unknown')}")
    except Exception as e:
        print(f"Server not reachable: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_update(args):
    """Self-update agenticore to the latest version."""
    import subprocess

    print(f"Current version: {__version__}")
    print("Updating agenticore...")

    source = args.source or "agenticore"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", source]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            # Re-read version after upgrade
            new_version = _get_installed_version()
            if new_version and new_version != __version__:
                print(f"Updated: {__version__} -> {new_version}")
            else:
                print("Already up to date.")
        else:
            print(f"Update failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Update timed out.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)


def _get_installed_version() -> str:
    """Read the installed version fresh (bypasses cached __version__)."""
    import importlib

    try:
        import agenticore as mod

        importlib.reload(mod)
        return mod.__version__
    except Exception:
        return ""


def _cmd_init_shared_fs(args):
    """Initialise shared FS layout."""
    import os
    from pathlib import Path

    shared_root = args.shared_root or os.getenv("AGENTICORE_SHARED_FS_ROOT", "")
    if not shared_root:
        print("Error: --shared-root or AGENTICORE_SHARED_FS_ROOT required", file=sys.stderr)
        sys.exit(1)

    root = Path(shared_root)

    # Create directory layout
    for subdir in ("profiles", "repos", "jobs", "job-state"):
        (root / subdir).mkdir(parents=True, exist_ok=True)
        print(f"  created {root / subdir}")

    print(f"\nShared FS initialised at: {root}")

    # Sync agentihooks if URL is configured
    agentihooks_url = os.getenv("AGENTICORE_AGENTIHOOKS_URL", "")
    if agentihooks_url:
        from agenticore.hooks import sync_agentihooks

        try:
            install_path = sync_agentihooks(agentihooks_url)
            if install_path:
                print(f"\nAgentihooks installed at: {install_path}")
        except Exception as e:
            print(f"\nWarning: agentihooks sync failed: {e}", file=sys.stderr)


def _cmd_hooks_sync(args):
    """Clone or update agentihooks repo."""
    import os

    from agenticore.hooks import sync_agentihooks

    url = args.url or os.getenv("AGENTICORE_AGENTIHOOKS_URL", "")
    try:
        install_path = sync_agentihooks(url)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if install_path:
        print(f"agentihooks installed at: {install_path}")
    else:
        print(
            "No agentihooks URL configured. Set AGENTICORE_AGENTIHOOKS_URL or pass --url.",
            file=sys.stderr,
        )
        sys.exit(1)


def _cmd_drain(args):
    """Mark this pod as draining and wait for in-progress jobs to finish."""
    import os
    import time

    pod_name = os.getenv("AGENTICORE_POD_NAME", "") or os.uname().nodename
    timeout = args.timeout

    print(f"Draining pod: {pod_name} (timeout={timeout}s)")

    # Mark pod as draining in Redis
    redis_url = os.getenv("REDIS_URL", "")
    r = None
    if redis_url:
        try:
            import redis as redis_lib

            client = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5.0)
            prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
            client.setex(f"{prefix}:pod:{pod_name}:draining", timeout, "1")
            r = client  # only assign if connection succeeded
            print("  marked draining in Redis")
        except Exception as e:
            print(f"  Redis unavailable ({e}), continuing without drain flag", file=sys.stderr)

    # Poll until no running jobs on this pod or timeout
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        from agenticore.jobs import list_jobs

        running = [j for j in list_jobs(limit=100, status="running") if j.pod_name == pod_name]
        if not running:
            print("All jobs complete. Pod ready to terminate.")
            break
        print(f"  waiting for {len(running)} job(s)...")
        time.sleep(5)
    else:
        print(f"Drain timeout ({timeout}s) reached — terminating anyway.", file=sys.stderr)

    # Remove draining flag
    if r:
        prefix = os.getenv("REDIS_KEY_PREFIX", "agenticore")
        r.delete(f"{prefix}:pod:{pod_name}:draining")


def _cmd_plan(args):
    """Submit a planning task."""
    payload = {
        "task": args.task,
        "repo_url": args.repo or "",
        "wait": args.wait,
    }
    if args.file_path:
        payload["file_path"] = args.file_path
    try:
        data = _api_post("/plans", payload)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if data.get("success"):
        print(f"Plan submitted: {data['plan_id']}")
        print(f"  Name:   {data.get('plan_name', '')}")
        print(f"  Job:    {data['job_id']}")
        print(f"  Status: {data['status']}")
        if args.wait and data.get("content"):
            print(f"\n{data['content']}")
    else:
        print(f"Error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


def _cmd_plans(args):
    """List recent plans."""
    try:
        data = _api_get(f"/plans?limit={args.limit}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not data.get("success"):
        print(f"Error: {data.get('error')}", file=sys.stderr)
        sys.exit(1)

    plans = data.get("plans", [])
    if not plans:
        print("No plans found.")
        return

    print(f"{'ID':<38} {'STATUS':<10} {'NAME':<35} {'TASK'}")
    print("-" * 100)
    for p in plans:
        task_short = p.get("task", "")[:30]
        print(f"{p['id']:<38} {p['status']:<10} {p.get('name', ''):<35} {task_short}")


def _cmd_execute_plan(args):
    """Execute a plan."""
    payload = {
        "repo_url": args.repo or "",
        "profile": args.profile or "",
        "wait": args.wait,
    }
    if args.file_path:
        payload["file_path"] = args.file_path
    try:
        data = _api_post(f"/plans/{args.plan_id}/execute", payload)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if data.get("success"):
        job = data["job"]
        print(f"Execution job submitted: {job['id']}")
        print(f"  Status:  {job['status']}")
        print(f"  Profile: {job.get('profile', '')}")
        if args.wait and job.get("output"):
            print(f"\n{job['output']}")
    else:
        print(f"Error: {data.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Container command utilities
# ─────────────────────────────────────────────────────────────────────────────


def _log_info(msg):
    print(f"\033[0;34m[INFO]\033[0m {msg}")


def _log_success(msg):
    print(f"\033[0;32m[SUCCESS]\033[0m {msg}")


def _log_warning(msg):
    print(f"\033[1;33m[WARNING]\033[0m {msg}")


def _log_error(msg):
    print(f"\033[0;31m[ERROR]\033[0m {msg}", file=sys.stderr)


def _load_env_file(env_path) -> dict:
    env_vars = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                env_vars[key.strip()] = value
    except Exception:
        pass
    return env_vars


def _run_cmd(cmd, check=True, env=None):
    import subprocess

    run_env = sys.modules["os"].environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(cmd, env=run_env, check=check)


def _check_docker() -> bool:
    import shutil

    if shutil.which("docker") is None:
        _log_error("Docker is not installed or not in PATH")
        return False
    return True


def _container_exists(name: str) -> bool:
    import subprocess

    r = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    return name in r.stdout.strip().split("\n")


def _container_running(name: str) -> bool:
    import subprocess

    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    return name in r.stdout.strip().split("\n")


def _find_file_up(filename: str, max_levels: int = 5):
    from pathlib import Path

    search = Path.cwd()
    for _ in range(max_levels):
        f = search / filename
        if f.exists():
            return f
        search = search.parent
    return None


def _resolve_container_name(args) -> str:
    import os
    from pathlib import Path

    if getattr(args, "agent_name", None):
        return args.agent_name
    if "AGENTICORE_AGENT" in os.environ:
        return os.environ["AGENTICORE_AGENT"]
    env_path = Path(getattr(args, "env", ".env"))
    if env_path.exists():
        env_vars = _load_env_file(env_path)
        if "AGENTICORE_AGENT" in env_vars:
            return env_vars["AGENTICORE_AGENT"]
    return "agenticore"


# ─────────────────────────────────────────────────────────────────────────────
# agent command
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_agent(args):
    """Build, run, and manage the agenticore container."""
    import subprocess
    import time
    from pathlib import Path

    if not _check_docker():
        sys.exit(1)

    if args.list:
        print("\nLocal containers:")
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True,
            text=True,
        )
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
        return

    name = _resolve_container_name(args)
    image_name = f"{name}:latest"
    _log_info(f"Container: {name}")

    if args.build:
        dockerfile = _find_file_up("Dockerfile")
        project_root = dockerfile.parent if dockerfile else Path.cwd()
        _log_info(f"Building {image_name} ...")
        _log_info(f"  Dockerfile : {dockerfile}")
        _log_info(f"  Context    : {project_root}")
        _run_cmd(["docker", "build", "-t", image_name, str(project_root)])
        _log_success(f"Built: {image_name}")

    if args.run:
        if _container_exists(name):
            if _container_running(name):
                _log_warning(f"Container '{name}' is already running")
                _log_info("Use --enter to access it or --stop to stop it first")
            else:
                _log_info("Removing existing stopped container...")
                _run_cmd(["docker", "rm", name], check=False)

        env_path = Path(getattr(args, "env", ".env"))
        dockerfile = _find_file_up("Dockerfile")
        project_root = dockerfile.parent if dockerfile else Path.cwd()

        cmd = [
            "docker", "run", "-d", "--name", name,
            "-p", "8200:8200",
            "-e", f"AGENTICORE_AGENT={name}",
        ]
        if env_path.exists():
            cmd.extend(["--env-file", str(env_path)])
            _log_info(f"Loading env: {env_path}")

        if args.dev:
            cmd.extend(["-e", "STORAGE_SYNC=false"])
            if not args.full and not getattr(args, "entrypoint", None):
                cmd.extend(["--entrypoint", "/bin/bash", "-it"])
                _log_info("Dev mode: bash entrypoint")
            else:
                _log_info("Full mode: image entrypoint")
            cmd.extend(["-v", f"{project_root}:/app:rw"])
            _log_info(f"Mounted: {project_root} -> /app")

        if getattr(args, "entrypoint", None):
            cmd.extend(["--entrypoint", args.entrypoint])

        final_image = getattr(args, "image", None) or image_name
        cmd.append(final_image)

        _run_cmd(cmd)
        time.sleep(2)

        if _container_running(name):
            _log_success(f"Container '{name}' started")
            _log_info(f"API   : http://localhost:8200")
            _log_info(f"Enter : agenticore agent --enter")
            _log_info(f"Logs  : agenticore agent --logs")
            _log_info(f"Stop  : agenticore agent --stop")
        else:
            _log_error("Container failed to start")
            _log_info(f"Check: docker logs {name}")
            sys.exit(1)

    if args.enter:
        if not _container_exists(name):
            _log_error(f"Container '{name}' does not exist")
            sys.exit(1)
        if not _container_running(name):
            _log_error(f"Container '{name}' is not running")
            sys.exit(1)
        try:
            _run_cmd(["docker", "exec", "-it", name, "bash"])
        except KeyboardInterrupt:
            pass

    if args.logs:
        if not _container_exists(name):
            _log_error(f"Container '{name}' does not exist")
            sys.exit(1)
        _log_info("Press Ctrl+C to exit")
        try:
            _run_cmd(["docker", "logs", "-f", name])
        except KeyboardInterrupt:
            pass

    if args.stop:
        if not _container_exists(name):
            _log_warning(f"Container '{name}' does not exist")
            return
        if _container_running(name):
            _run_cmd(["docker", "stop", name], check=False)
            _log_success("Container stopped")
        _run_cmd(["docker", "rm", name], check=False)
        _log_success("Container removed")


# ─────────────────────────────────────────────────────────────────────────────
# push command
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_push(args):
    """Build and push the main Docker image to a registry."""
    import os
    from pathlib import Path

    registry = os.getenv("DOCKER_REGISTRY", "")
    if not registry:
        _log_error("DOCKER_REGISTRY environment variable not set")
        sys.exit(1)

    if not _check_docker():
        sys.exit(1)

    if not args.main and not args.all:
        _log_error("No image specified. Use --main or --all")
        print("\nUsage:")
        print("  agenticore push --main    Build and push main image")
        print("  agenticore push --all     Build and push all images")
        sys.exit(1)

    tag = args.tag
    dockerfile = _find_file_up("Dockerfile")
    project_root = dockerfile.parent if dockerfile else Path.cwd()
    full_image = f"{registry}/agenticore:{tag}"

    _log_info(f"Registry : {registry}")
    _log_info(f"Tag      : {tag}")
    _log_info(f"Image    : {full_image}")
    print()

    if not args.push_only:
        _log_info(f"Building {full_image} ...")
        cmd = ["docker", "build", "-t", full_image, str(project_root)]
        if args.no_cache:
            cmd.insert(2, "--no-cache")
        _run_cmd(cmd)
        _log_success(f"Built: {full_image}")

    if not args.build_only:
        _log_info(f"Pushing {full_image} ...")
        _run_cmd(["docker", "push", full_image])
        _log_success(f"Pushed: {full_image}")

    print()
    _log_success("Done!")


def _cmd_version(args):
    print(f"agenticore {__version__}")


def main():
    parser = argparse.ArgumentParser(
        prog="agenticore",
        description="Claude Code runner and orchestrator",
    )
    parser.add_argument("--version", action="version", version=f"agenticore {__version__}")
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Submit a task")
    p_run.add_argument("task", help="Task description")
    p_run.add_argument("--repo", "-r", help="GitHub repo URL")
    p_run.add_argument("--profile", "-p", help="Execution profile")
    p_run.add_argument("--base-ref", default="main", help="Base branch (default: main)")
    p_run.add_argument("--wait", "-w", action="store_true", help="Wait for completion")
    p_run.add_argument("--session-id", help="Claude session ID to resume")
    p_run.add_argument("--file-path", dest="file_path", help="Path to .mcp.json to inject into job config")
    p_run.set_defaults(func=_cmd_run)

    # serve
    p_serve = sub.add_parser("serve", help="Start the server")
    p_serve.add_argument("--port", type=int, help="Server port")
    p_serve.add_argument("--host", help="Bind address")
    p_serve.set_defaults(func=_cmd_serve)

    # jobs
    p_jobs = sub.add_parser("jobs", help="List recent jobs")
    p_jobs.add_argument("--limit", "-n", type=int, default=20, help="Max jobs")
    p_jobs.add_argument("--status", "-s", help="Filter by status")
    p_jobs.set_defaults(func=_cmd_jobs)

    # job
    p_job = sub.add_parser("job", help="Get job details")
    p_job.add_argument("job_id", help="Job UUID")
    p_job.add_argument("--json", action="store_true", help="Output as JSON")
    p_job.set_defaults(func=_cmd_job)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a job")
    p_cancel.add_argument("job_id", help="Job UUID")
    p_cancel.set_defaults(func=_cmd_cancel)

    # profiles
    p_profiles = sub.add_parser("profiles", help="List profiles")
    p_profiles.set_defaults(func=_cmd_profiles)

    # status
    p_status = sub.add_parser("status", help="Check server health")
    p_status.set_defaults(func=_cmd_status)

    # update
    p_update = sub.add_parser("update", help="Update agenticore to latest version")
    p_update.add_argument(
        "--source",
        help="Install source (default: agenticore from PyPI, or a git URL / local path)",
    )
    p_update.set_defaults(func=_cmd_update)

    # version
    p_version = sub.add_parser("version", help="Show version")
    p_version.set_defaults(func=_cmd_version)

    # init-shared-fs
    p_init = sub.add_parser("init-shared-fs", help="Initialise shared FS layout (Kubernetes)")
    p_init.add_argument(
        "--shared-root",
        help="Shared FS root path (default: AGENTICORE_SHARED_FS_ROOT env var)",
    )
    p_init.set_defaults(func=_cmd_init_shared_fs)

    # drain
    p_drain = sub.add_parser("drain", help="Drain pod: wait for in-progress jobs to finish")
    p_drain.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Max seconds to wait for jobs (default: 300)",
    )
    p_drain.set_defaults(func=_cmd_drain)

    # plan
    p_plan = sub.add_parser("plan", help="Create an implementation plan (read-only analysis)")
    p_plan.add_argument("task", help="Task description to plan")
    p_plan.add_argument("--repo", "-r", help="GitHub repo URL to analyse")
    p_plan.add_argument("--wait", "-w", action="store_true", help="Wait for plan to complete")
    p_plan.add_argument("--file-path", dest="file_path", help="Path to .mcp.json to inject into plan job config")
    p_plan.set_defaults(func=_cmd_plan)

    # plans
    p_plans = sub.add_parser("plans", help="List recent plans")
    p_plans.add_argument("--limit", "-n", type=int, default=20, help="Max plans")
    p_plans.set_defaults(func=_cmd_plans)

    # execute-plan
    p_exec_plan = sub.add_parser("execute-plan", help="Execute a ready plan")
    p_exec_plan.add_argument("plan_id", help="Plan UUID")
    p_exec_plan.add_argument("--repo", "-r", help="Override repo URL")
    p_exec_plan.add_argument("--profile", "-p", help="Execution profile")
    p_exec_plan.add_argument("--wait", "-w", action="store_true", help="Wait for completion")
    p_exec_plan.add_argument(
        "--file-path", dest="file_path", help="Path to .mcp.json to inject into execution job config"
    )
    p_exec_plan.set_defaults(func=_cmd_execute_plan)

    # agent
    p_agent = sub.add_parser("agent", help="Build, run, and manage the agenticore container")
    p_agent.add_argument("agent_name", nargs="?", help="Container name (or set AGENTICORE_AGENT)")
    p_agent.add_argument("--build", "-b", action="store_true", help="Build the Docker image")
    p_agent.add_argument("--run", "-r", action="store_true", help="Run container in detached mode")
    p_agent.add_argument("--enter", "-e", action="store_true", help="Shell into running container")
    p_agent.add_argument("--stop", "-s", action="store_true", help="Stop and remove the container")
    p_agent.add_argument("--logs", "-l", action="store_true", help="Follow container logs")
    p_agent.add_argument("--dev", "-d", action="store_true", help="Dev mode: mount source, disable sync")
    p_agent.add_argument("--full", "-f", action="store_true", help="Keep image entrypoint when using --dev")
    p_agent.add_argument("--list", action="store_true", help="List local containers")
    p_agent.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    p_agent.add_argument("--entrypoint", help="Custom container entrypoint")
    p_agent.add_argument("--image", help="Custom image to run (NAME:TAG)")
    p_agent.set_defaults(func=_cmd_agent)

    # push
    p_push = sub.add_parser("push", help="Build and push Docker image to registry")
    p_push.add_argument("--main", "-m", action="store_true", help="Build and push main image")
    p_push.add_argument("--all", "-a", action="store_true", help="Build and push all images (same as --main)")
    p_push.add_argument("--tag", "-t", default="latest", help="Image tag (default: latest)")
    p_push.add_argument("--build-only", action="store_true", help="Only build, don't push")
    p_push.add_argument("--push-only", action="store_true", help="Only push (assumes image built)")
    p_push.add_argument("--no-cache", action="store_true", help="Build without cache")
    p_push.set_defaults(func=_cmd_push)

    # hooks
    p_hooks = sub.add_parser("hooks", help="Manage agentihooks integration")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")
    p_hooks_sync = hooks_sub.add_parser("sync", help="Clone or update agentihooks repo")
    p_hooks_sync.add_argument(
        "--url",
        help="Git URL to clone (overrides AGENTICORE_AGENTIHOOKS_URL)",
    )
    p_hooks_sync.set_defaults(func=_cmd_hooks_sync)

    def _cmd_hooks_default(args):
        p_hooks.print_help()
        sys.exit(0)

    p_hooks.set_defaults(func=_cmd_hooks_default)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
