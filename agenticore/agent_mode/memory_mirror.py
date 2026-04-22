"""Memory mirror bootstrap — runs once per agent boot when
``MEMORY_MIRROR_ROLE`` is set to a non-off value.

Responsibilities:
  1. Write a credential helper that mints fresh GitHub App installation tokens
     (the pod's cached GITHUB_TOKEN may be stale when the mirror repo was
     granted access after the token was issued).
  2. Register the helper with git via ``credential.helper``.
  3. Ensure ``~/.agentihooks/.env`` declares MEMORY_MIRROR_ROLE +
     MEMORY_MIRROR_REMOTE so the agentihooks CLI picks up the config.
  4. Run one ``memory-sync sync-now`` to pull initial state from origin/main.
  5. Loop: sleep(interval) + sync-now. Runs forever in a background daemon
     thread, exits silently on process shutdown.

All env-var driven — no agent-specific config, no per-package runners. The
feature is default-off: unset ``MEMORY_MIRROR_ROLE`` and this module no-ops.

Env vars:
  MEMORY_MIRROR_ROLE               off | consumer | offline | contributor | authority
  MEMORY_MIRROR_REMOTE             git URL of the private mirror repo (required)
  MEMORY_MIRROR_TICK_INTERVAL_SEC  seconds between ticks (default 60)
  GITHUB_APP_ID / _INSTALLATION_ID / _PRIVATE_KEY
                                   used by the credential helper
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

_log = logging.getLogger(__name__)

_HELPER_PATH = Path.home() / ".agenticore-github-token-helper.py"
_AGENTIHOOKS_ENV = Path.home() / ".agentihooks" / ".env"

# Self-contained helper. Written to disk, invoked by git via
# ``credential.helper``. Must not import anything outside the stdlib + jwt +
# httpx (both already in the agenticore image).
_HELPER_SOURCE = '''#!/usr/bin/env python3
"""Mint a fresh GitHub App installation token and emit git-credential output.

git calls this as `credential.helper "!python3 <path>"`. We print
    username=x-access-token
    password=<fresh_token>
on stdout — git consumes those two lines and fills the HTTPS auth prompt.
"""
import os
import sys
import time

try:
    import jwt
    import httpx
except ImportError as exc:
    print(f"# missing dep: {exc}", file=sys.stderr)
    sys.exit(1)

pk = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
app_id = os.environ.get("GITHUB_APP_ID", "")
inst = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
if not (pk and app_id and inst):
    print("# GITHUB_APP_* env vars missing; cannot mint token", file=sys.stderr)
    sys.exit(1)

now = int(time.time())
j = jwt.encode(
    {"iat": now - 60, "exp": now + 540, "iss": app_id},
    pk,
    algorithm="RS256",
)
try:
    r = httpx.post(
        f"https://api.github.com/app/installations/{inst}/access_tokens",
        headers={"Authorization": f"Bearer {j}", "Accept": "application/vnd.github+json"},
        timeout=15.0,
    )
    r.raise_for_status()
except Exception as exc:
    print(f"# token exchange failed: {exc}", file=sys.stderr)
    sys.exit(1)

tok = r.json().get("token")
if not tok:
    print("# token exchange returned no token", file=sys.stderr)
    sys.exit(1)

print("username=x-access-token")
print(f"password={tok}")
'''


def _role() -> str:
    return (os.environ.get("MEMORY_MIRROR_ROLE", "") or "off").strip().lower()


def _remote() -> str:
    return (os.environ.get("MEMORY_MIRROR_REMOTE", "") or "").strip()


def _interval() -> int:
    try:
        return max(30, int(os.environ.get("MEMORY_MIRROR_TICK_INTERVAL_SEC", "60")))
    except ValueError:
        return 60


def _write_helper() -> None:
    _HELPER_PATH.write_text(_HELPER_SOURCE, encoding="utf-8")
    _HELPER_PATH.chmod(_HELPER_PATH.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _wire_git_credential() -> None:
    helper_cmd = f"!f() {{ python3 {_HELPER_PATH}; }}; f"
    subprocess.run(
        ["git", "config", "--global", "credential.helper", helper_cmd],
        check=False,
        capture_output=True,
    )


def _ensure_env_line(key: str, value: str) -> None:
    """Append ``KEY=value`` to ~/.agentihooks/.env if no line starts with KEY=."""
    _AGENTIHOOKS_ENV.parent.mkdir(parents=True, exist_ok=True)
    _AGENTIHOOKS_ENV.touch(exist_ok=True)
    existing = _AGENTIHOOKS_ENV.read_text(encoding="utf-8")
    for line in existing.splitlines():
        if line.strip().startswith(f"{key}="):
            return
    with _AGENTIHOOKS_ENV.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{key}={value}\n")


def _resolve_agentihooks_python_invocation() -> list[str] | None:
    """Return the argv prefix to invoke agentihooks memory-sync.

    Tries in order:
      1. Local checkout at ``AGENTICORE_AGENTIHOOKS_PATH`` or the default
         ``/shared/agentihooks`` — invoked as ``python -m scripts.install``.
         This is the authoritative path when the URL-override feature is
         in use, because it carries the latest dev commit.
      2. The installed CLI on PATH — ``agentihooks`` — used only as fallback
         when no checkout exists (the CLI shipped in PyPI may or may not
         have the ``memory-sync`` subcommand depending on version).

    Returns ``None`` if no usable invocation exists.
    """
    candidate_paths: list[Path] = []
    env_path = os.environ.get("AGENTICORE_AGENTIHOOKS_PATH", "").strip()
    if env_path:
        candidate_paths.append(Path(env_path))
    candidate_paths.append(Path("/shared/agentihooks"))
    for path in candidate_paths:
        if (path / "scripts" / "install.py").is_file():
            return [sys.executable, "-m", "scripts.install"]
    # Fallback: the installed CLI. May or may not have memory-sync — we just
    # try. If it doesn't, the sync call will error and the loop will log it.
    from shutil import which

    cli = which("agentihooks")
    if cli:
        return [cli]
    return None


def _run_sync(argv_prefix: list[str], cwd: Path | None) -> subprocess.CompletedProcess:
    cmd = argv_prefix + ["memory-sync", "sync-now"]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cwd) if cwd else None,
    )


def _install_prereqs(role: str, remote: str) -> None:
    _write_helper()
    _wire_git_credential()
    _ensure_env_line("MEMORY_MIRROR_ROLE", role)
    _ensure_env_line("MEMORY_MIRROR_REMOTE", remote)


def _tick_loop(argv_prefix: list[str], interval: int) -> None:
    """Blocking loop — run in a daemon thread. Never raises."""
    # Invoke from the checkout dir when one exists so ``-m scripts.install``
    # resolves properly; otherwise cwd is irrelevant.
    cwd: Path | None = None
    for candidate in (
        os.environ.get("AGENTICORE_AGENTIHOOKS_PATH", "").strip() or None,
        "/shared/agentihooks",
    ):
        if candidate and (Path(candidate) / "scripts" / "install.py").is_file():
            cwd = Path(candidate)
            break

    role = _role()
    _log.info("memory-mirror tick loop entering (interval=%ds, cwd=%s, role=%s)", interval, cwd, role)
    tick_count = 0
    ok_count = 0
    fail_count = 0
    consecutive_failures = 0
    fail_threshold = max(2, int(os.environ.get("MEMORY_MIRROR_ALERT_AFTER", "3")))
    while True:
        tick_count += 1
        ok = False
        try:
            proc = _run_sync(argv_prefix, cwd)
            if proc.returncode != 0:
                _log.warning(
                    "memory-mirror tick #%d exit=%d: %s",
                    tick_count,
                    proc.returncode,
                    (proc.stderr or proc.stdout or "").strip()[:400],
                )
            else:
                tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
                _log.info("memory-mirror tick #%d ok: %s", tick_count, tail[0][:200])
                ok = True
        except Exception as exc:
            _log.warning("memory-mirror tick #%d error: %s", tick_count, exc)

        if ok:
            ok_count += 1
            if consecutive_failures >= fail_threshold:
                _log.info(
                    "memory-mirror RECOVERED after %d consecutive failures", consecutive_failures
                )
            consecutive_failures = 0
        else:
            fail_count += 1
            consecutive_failures += 1
            if consecutive_failures == fail_threshold:
                _log.error(
                    "memory-mirror DEGRADED: %d consecutive tick failures — "
                    "check git credentials / remote reachability",
                    consecutive_failures,
                )
        # Structured metric line — grep-able by loki / OTEL log-to-metric rules.
        # Format: memory_mirror_metric role=<r> ticks=<n> ok=<n> fail=<n> consecutive_failures=<n>
        _log.info(
            "memory_mirror_metric role=%s ticks=%d ok=%d fail=%d consecutive_failures=%d",
            role,
            tick_count,
            ok_count,
            fail_count,
            consecutive_failures,
        )
        time.sleep(interval)


def _seed_own_project_dir() -> None:
    """Pre-create ``~/.claude/projects/<encoded>/memory/`` for this agent's
    own package directory, so the first consume tick has a target to
    populate on a fresh PVC.

    The encoding mirrors Claude Code's: absolute path with ``/`` → ``-``
    and a leading ``-``. Idempotent — only mkdirs, never overwrites.
    """
    # Resolve this agent's package dir. On anton dev pods it's
    # /shared/agentihub/agents/<agent>/package; config exposes it as
    # AGENT_MODE_PACKAGE_DIR via the agenticore config loader.
    from agenticore.config import get_config
    cfg = get_config()
    pkg = getattr(getattr(cfg, "agent_mode", None), "package_dir", None)
    if not pkg:
        _log.debug("cold-seed: package_dir unresolved — skipping")
        return
    pkg_abs = str(Path(pkg).resolve())
    encoded = pkg_abs.replace("/", "-")
    if not encoded.startswith("-"):
        encoded = "-" + encoded
    home = Path(os.environ.get("HOME") or Path.home())
    target = home / ".claude" / "projects" / encoded / "memory"
    target.mkdir(parents=True, exist_ok=True)
    _log.info("cold-seed: ensured %s", target)


def setup_memory_mirror() -> None:
    """Entry point. Called as a background daemon thread from
    ``initialize_agent_mode``. Safe to call regardless of config — self-gates
    on ``MEMORY_MIRROR_ROLE``.
    """
    role = _role()
    if role == "off":
        _log.debug("Memory mirror: MEMORY_MIRROR_ROLE unset/off — skipping")
        return
    remote = _remote()
    if not remote:
        _log.warning(
            "Memory mirror: MEMORY_MIRROR_ROLE=%s but MEMORY_MIRROR_REMOTE unset — skipping",
            role,
        )
        return
    argv_prefix = _resolve_agentihooks_python_invocation()
    if argv_prefix is None:
        _log.warning("Memory mirror: cannot locate agentihooks — skipping")
        return

    try:
        _install_prereqs(role, remote)
    except Exception as exc:
        _log.warning("Memory mirror prereq install failed: %s", exc)
        return

    # Cold-PVC seed — consume_main walks ``~/.claude/projects/<encoded>/`` to
    # decide where to drop memory. On a fresh PVC that dir doesn't exist yet
    # (Claude Code hasn't run), so the first tick merges 0 projects and
    # memory never lands until a human session happens. Pre-create the dir
    # for this agent's own package so the mirror has a target to populate.
    try:
        _seed_own_project_dir()
    except Exception as exc:
        _log.warning("Memory mirror cold-seed failed (non-fatal): %s", exc)

    _log.info("Memory mirror: role=%s remote=%s — running initial sync", role, remote)
    try:
        proc = _run_sync(argv_prefix, cwd=None if argv_prefix[0].endswith("agentihooks") else Path("/shared/agentihooks"))
        if proc.returncode != 0:
            _log.warning(
                "Initial memory-sync exit=%d: %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[:400],
            )
        else:
            _log.info("Initial memory-sync complete")
    except Exception as exc:
        _log.warning("Initial memory-sync failed: %s", exc)

    interval = _interval()
    _log.info("Memory mirror: tick loop started (interval=%ds)", interval)
    _tick_loop(argv_prefix, interval)
