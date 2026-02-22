"""CLI entrypoint for Agenticore.

Usage::

    agenticore version
    agenticore status
    agenticore run [--port PORT]
    agenticore help
"""

import sys

from agenticore import __version__


def _cmd_version():
    print(f"agenticore {__version__}")


def _cmd_help():
    print(
        f"""agenticore {__version__} — Claude Code runner and orchestrator

Usage:
  agenticore version    Show version
  agenticore status     Show server status
  agenticore run        Start the server
  agenticore help       Show this help

Environment:
  AGENTICORE_TRANSPORT  stdio (default) or sse
  AGENTICORE_PORT       Server port (default: 8200)
  AGENTICORE_HOST       Bind address (default: 127.0.0.1)
"""
    )


def _cmd_status():
    import os

    try:
        import httpx

        host = os.getenv("AGENTICORE_HOST", "127.0.0.1")
        port = os.getenv("AGENTICORE_PORT", "8200")
        resp = httpx.get(f"http://{host}:{port}/health", timeout=3)
        data = resp.json()
        print(f"Status: {data.get('status', 'unknown')}")
        print(f"Service: {data.get('service', 'unknown')}")
    except Exception as e:
        print(f"Server not reachable: {e}")
        sys.exit(1)


def _cmd_run():
    from agenticore.server import main

    main()


def main():
    args = sys.argv[1:]

    if not args or args[0] == "help":
        _cmd_help()
    elif args[0] == "version":
        _cmd_version()
    elif args[0] == "status":
        _cmd_status()
    elif args[0] == "run":
        _cmd_run()
    else:
        print(f"Unknown command: {args[0]}", file=sys.stderr)
        _cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
