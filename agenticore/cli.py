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
        print(f"  Profile: {job.get('profile', 'code')}")
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
        job = data["job"]
        print(f"Job:     {job['id']}")
        print(f"Status:  {job['status']}")
        print(f"Profile: {job.get('profile', '')}")
        print(f"Task:    {job.get('task', '')}")
        if job.get("repo_url"):
            print(f"Repo:    {job['repo_url']}")
        if job.get("exit_code") is not None:
            print(f"Exit:    {job['exit_code']}")
        if job.get("pr_url"):
            print(f"PR:      {job['pr_url']}")
        if job.get("created_at"):
            print(f"Created: {job['created_at']}")
        if job.get("ended_at"):
            print(f"Ended:   {job['ended_at']}")
        if job.get("error"):
            print(f"\nError:\n{job['error']}")
        if job.get("output"):
            print(f"\nOutput:\n{job['output'][:2000]}")


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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
