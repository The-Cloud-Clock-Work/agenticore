"""Agenticore background daemon.

Manages a long-running background process for agenticore.
Started automatically by `agenticore init`, or manually via `agenticore daemon start`.

The daemon polls on a configurable interval. The _poll_cycle() function is a
placeholder for future logic (e.g., agentihub sync, pod health checks).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".agenticore"
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"
STATE_FILE = STATE_DIR / "state.json"
POLL_INTERVAL = int(os.environ.get("AGENTICORE_DAEMON_POLL_SEC", "60"))


def _log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line)


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
    return None


def _read_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _poll_cycle():
    """Daemon work per cycle. Override with real logic later."""
    pass


def start() -> int | None:
    """Start the daemon. Returns PID if started, None if already running."""
    existing = _read_pid()
    if existing:
        return None

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "agenticore.daemon", "_run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def run_foreground():
    """Main daemon loop — called after detach."""
    PID_FILE.write_text(str(os.getpid()))
    _log("daemon started")

    def _shutdown(signum, frame):
        _log("daemon stopping")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True:
            _poll_cycle()
            time.sleep(POLL_INTERVAL)
    except Exception as e:
        _log(f"daemon error: {e}")
    finally:
        PID_FILE.unlink(missing_ok=True)


def stop() -> bool:
    """Stop the daemon. Returns True if stopped, False if not running."""
    pid = _read_pid()
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    return True


def status() -> dict:
    """Return daemon status as a dict."""
    pid = _read_pid()
    return {
        "running": pid is not None,
        "pid": pid,
        "pid_file": str(PID_FILE),
        "log_file": str(LOG_FILE),
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        pid = start()
        if pid:
            print(f"Daemon started (PID {pid})")
        else:
            print(f"Daemon already running (PID {_read_pid()})")
    elif cmd == "stop":
        if stop():
            print("Daemon stopped")
        else:
            print("Daemon not running")
    elif cmd == "status":
        s = status()
        if s["running"]:
            print(f"Running (PID {s['pid']})")
        else:
            print("Not running")
    elif cmd == "_run":
        run_foreground()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
