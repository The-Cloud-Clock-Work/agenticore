"""Agentihooks integration — clone/fetch and build profiles.

Handles acquiring the agentihooks repository and invoking its own build
tooling so agenticore always has up-to-date profiles available.

Install directory resolution::

    AGENTICORE_AGENTIHOOKS_PATH explicitly set  →  use as-is (no cloning)
    AGENTICORE_AGENTIHOOKS_URL set:
      AGENTICORE_SHARED_FS_ROOT set            →  {shared_fs_root}/agentihooks
      else                                     →  ~/.agenticore/agentihooks
"""

import fcntl
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.git_credentials import git_askpass_env
from agenticore.repos import _run_git, _with_redis_lock, resolve_github_token

logger = logging.getLogger(__name__)


def _install_dir() -> Path:
    """Determine where agentihooks should be installed.

    Checks AGENTICORE_AGENTIHOOKS_PATH first (explicit override), then
    AGENTICORE_SHARED_FS_ROOT for K8s deployments, then local default.
    """
    explicit = os.getenv("AGENTICORE_AGENTIHOOKS_PATH", "")
    if explicit:
        return Path(explicit)
    shared = os.getenv("AGENTICORE_SHARED_FS_ROOT", "")
    if shared:
        return Path(shared) / "agentihooks"
    return Path.home() / ".agenticore" / "agentihooks"


def _run_build(install_dir: Path) -> None:
    """Invoke agentihooks' own build script.

    Passes AGENTIHOOKS_HOME so agentihooks knows its install location
    (replacing the old /app hard-coded assumption).
    """
    build_script = install_dir / "scripts" / "build_profiles.py"
    if not build_script.exists():
        logger.warning("agentihooks build script not found at %s — skipping", build_script)
        return
    env = os.environ.copy()
    env["AGENTIHOOKS_HOME"] = str(install_dir)
    result = subprocess.run(
        [sys.executable, str(build_script)],
        cwd=str(install_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "agentihooks build failed (profiles may lack settings.json):\n%s",
            result.stderr,
        )
    else:
        logger.info("agentihooks profiles built at %s", install_dir)


def _clone_or_fetch(url: str, dest: Path) -> None:
    """Clone or update agentihooks repo, flock/Redis-protected."""
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / ".agentihooks.lock"

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
            else:
                _run_git(["git", "clone", url, str(dest)], extra_env=extra_env)
        _run_build(dest)

    if get_config().repos.shared_fs_root:
        _with_redis_lock("agenticore:lock:agentihooks", _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def start_sync_watcher(url: str, dest: Path, interval: int) -> threading.Thread:
    """Start a daemon thread that periodically git-fetches + rebuilds agentihooks.

    Args:
        url:      Agentihooks git URL (authenticated at call time by _clone_or_fetch).
        dest:     Install directory (already cloned).
        interval: Seconds between re-syncs. Must be > 0.

    Returns the started Thread (daemon, so it dies with the process).
    """

    def _watch():
        while True:
            time.sleep(interval)
            try:
                _clone_or_fetch(url, dest)
                logger.info("agentihooks hot-reload complete (%s)", dest)
            except Exception as exc:
                logger.warning("agentihooks hot-reload failed: %s", exc)

    t = threading.Thread(target=_watch, name="agentihooks-watcher", daemon=True)
    t.start()
    logger.info("agentihooks watcher started (interval=%ds, dest=%s)", interval, dest)
    return t


def _mcp_lib_install_dir() -> Path:
    """Determine where the MCP library repo should be installed.

    3-tier resolution: explicit env → shared FS → local default.
    """
    explicit = os.getenv("AGENTICORE_MCP_LIB_PATH", "")
    if explicit:
        return Path(explicit)
    shared = os.getenv("AGENTICORE_SHARED_FS_ROOT", "")
    if shared:
        return Path(shared) / "mcp-lib"
    return Path.home() / ".agenticore" / "mcp-lib"


def sync_mcp_lib(url: str = "") -> Optional[Path]:
    """Clone/fetch MCP library repo. Sets AGENTICORE_MCP_LIB_PATH in-process.

    Returns the install directory, or None if no URL is configured.
    If AGENTICORE_MCP_LIB_PATH is already set and no URL is provided,
    returns the existing path without cloning.
    """
    url = url or get_config().mcp_lib_url
    if not url:
        explicit = os.getenv("AGENTICORE_MCP_LIB_PATH")
        if explicit:
            return Path(explicit)
        return None
    dest = _mcp_lib_install_dir()
    _clone_or_fetch(url, dest)
    os.environ["AGENTICORE_MCP_LIB_PATH"] = str(dest)
    logger.info("AGENTICORE_MCP_LIB_PATH → %s", dest)
    return dest


def start_mcp_lib_watcher(url: str, dest: Path, interval: int) -> threading.Thread:
    """Background daemon thread that periodically re-fetches the MCP lib repo.

    Args:
        url:      MCP lib git URL.
        dest:     Install directory (already cloned).
        interval: Seconds between re-syncs. Must be > 0.

    Returns the started Thread (daemon, so it dies with the process).
    """

    def _watch():
        while True:
            time.sleep(interval)
            try:
                _clone_or_fetch(url, dest)
                logger.info("mcp-lib hot-reload complete (%s)", dest)
            except Exception as exc:
                logger.warning("mcp-lib hot-reload failed: %s", exc)

    t = threading.Thread(target=_watch, name="mcp-lib-watcher", daemon=True)
    t.start()
    logger.info("mcp-lib watcher started (interval=%ds, dest=%s)", interval, dest)
    return t


def _agentihub_install_dir() -> Path:
    """Determine where agentihub should be installed.

    3-tier resolution: explicit env → shared FS → local default.
    """
    explicit = os.getenv("AGENTIHUB_PATH", "")
    if explicit:
        return Path(explicit)
    shared = os.getenv("AGENTICORE_SHARED_FS_ROOT", "")
    if shared:
        return Path(shared) / "agentihub"
    return Path.home() / ".agenticore" / "agentihub"


def _run_agent_hub(agentihub_dir: Path, agentihooks_dir: Path) -> None:
    """Invoke agentihooks' agent_hub.py to copy hub agents into profiles.

    agent_hub.py reads AGENTIHUB_PATH to find agent definitions and copies
    them into the agentihooks profiles directory.
    """
    hub_script = agentihooks_dir / "scripts" / "agent_hub.py"
    if not hub_script.exists():
        logger.warning("agent_hub.py not found at %s — skipping hub sync", hub_script)
        return
    env = os.environ.copy()
    env["AGENTIHUB_PATH"] = str(agentihub_dir)
    env["AGENTIHOOKS_HOME"] = str(agentihooks_dir)
    result = subprocess.run(
        [sys.executable, str(hub_script)],
        cwd=str(agentihooks_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning(
            "agent_hub.py failed (hub agents may be missing):\n%s",
            result.stderr,
        )
    else:
        logger.info("agent_hub sync complete (%s → %s)", agentihub_dir, agentihooks_dir)


def _clone_or_fetch_agentihub(url: str, dest: Path) -> None:
    """Clone or update agentihub repo, then run agent_hub + rebuild profiles."""
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / ".agentihub.lock"

    agentihooks_dir = _install_dir()

    def _do():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if (dest / ".git").exists():
                _run_git(["git", "-C", str(dest), "fetch", "--all", "--prune"], extra_env=extra_env)
                _run_git(["git", "-C", str(dest), "reset", "--hard", "origin/HEAD"], extra_env=extra_env)
            else:
                _run_git(["git", "clone", url, str(dest)], extra_env=extra_env)
        _run_agent_hub(dest, agentihooks_dir)
        _run_build(agentihooks_dir)

    if get_config().repos.shared_fs_root:
        _with_redis_lock("agenticore:lock:agentihub", _do)
    else:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                _do()
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def sync_agentihub(url: str = "") -> Optional[Path]:
    """Clone/fetch agentihub + run agent_hub + rebuild profiles.

    Sets AGENTIHUB_PATH in-process. Returns the install directory,
    or None if no URL is configured.
    """
    url = url or get_config().agentihub_url
    if not url:
        explicit = os.getenv("AGENTIHUB_PATH")
        if explicit:
            return Path(explicit)
        return None
    dest = _agentihub_install_dir()
    _clone_or_fetch_agentihub(url, dest)
    os.environ["AGENTIHUB_PATH"] = str(dest)
    logger.info("AGENTIHUB_PATH → %s", dest)
    return dest


def start_agentihub_watcher(url: str, dest: Path, agentihooks_dir: Path, interval: int) -> threading.Thread:
    """Background daemon thread that periodically re-fetches agentihub.

    After each fetch: runs agent_hub.py → rebuilds agentihooks profiles.

    Args:
        url:             Agentihub git URL.
        dest:            Agentihub install directory (already cloned).
        agentihooks_dir: Agentihooks install directory.
        interval:        Seconds between re-syncs. Must be > 0.

    Returns the started Thread (daemon, so it dies with the process).
    """

    def _watch():
        while True:
            time.sleep(interval)
            try:
                _clone_or_fetch_agentihub(url, dest)
                logger.info("agentihub hot-reload complete (%s)", dest)
            except Exception as exc:
                logger.warning("agentihub hot-reload failed: %s", exc)

    t = threading.Thread(target=_watch, name="agentihub-watcher", daemon=True)
    t.start()
    logger.info("agentihub watcher started (interval=%ds, dest=%s)", interval, dest)
    return t


def sync_agentihooks(url: str = "") -> Optional[Path]:
    """Clone/fetch + build agentihooks. Sets AGENTICORE_AGENTIHOOKS_PATH in-process.

    Returns the install directory, or None if no URL is configured.
    If AGENTICORE_AGENTIHOOKS_PATH is already set and no URL is provided,
    returns the existing path without cloning.
    """
    url = url or get_config().agentihooks_url
    if not url:
        explicit = os.getenv("AGENTICORE_AGENTIHOOKS_PATH")
        if explicit:
            return Path(explicit)
        return None
    dest = _install_dir()
    _clone_or_fetch(url, dest)
    os.environ["AGENTICORE_AGENTIHOOKS_PATH"] = str(dest)
    logger.info("AGENTICORE_AGENTIHOOKS_PATH → %s", dest)
    return dest
