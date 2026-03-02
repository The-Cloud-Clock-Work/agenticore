"""Repository cloning and caching with flock-based serialization.

Layout::

    {repos_root}/
    └── {sha256(url)[:12]}/
        ├── .lock
        └── repo/

Clone once, ``git fetch --all`` on re-use. Claude ``--worktree`` handles
worktree creation inside the repo.
"""

import fcntl
import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.git_credentials import git_askpass_env, sanitize_remote_url, strip_credentials_from_url

logger = logging.getLogger(__name__)


def _repo_key(repo_url: str) -> str:
    """Deterministic short key for a repo URL."""
    return hashlib.sha256(repo_url.encode()).hexdigest()[:12]


def _repos_root() -> Path:
    cfg = get_config()
    return Path(cfg.repos.root)


def repo_dir(repo_url: str) -> Path:
    """Return the path to the cloned repo directory."""
    return _repos_root() / _repo_key(repo_url) / "repo"


def resolve_github_token() -> Optional[str]:
    """Resolve a GitHub token using the priority chain.

    1. GitHub App (app_id + private key + installation_id)
    2. Auth Broker (AUTH_BROKER_URL, service="github")
    3. Static GITHUB_TOKEN
    4. None (public repos only, no PRs)
    """
    # 1. GitHub App
    try:
        from agenticore.github_app import get_github_app_auth

        app = get_github_app_auth()
        if app.enabled:
            token = app.get_token()
            if token:
                logger.debug("github auth: using GitHub App installation token")
                return token
            logger.debug("github auth: GitHub App configured but token exchange failed, falling through")
    except Exception as exc:
        logger.debug("github auth: GitHub App error: %s", exc)

    # 2. Auth Broker
    cfg = get_config()
    if cfg.auth_broker.url:
        try:
            from agenticore.auth_client import AuthClient

            client = AuthClient()
            if client.enabled:
                cred = client.get_credential("github")
                if cred:
                    logger.debug("github auth: using Auth Broker token")
                    return cred
        except Exception as exc:
            logger.debug("github auth: Auth Broker error: %s", exc)

    # 3. Static token
    if cfg.github.token:
        logger.debug("github auth: using static GITHUB_TOKEN")
        return cfg.github.token

    # 4. None
    logger.debug("github auth: no token available (public repos only)")
    return None


def _redis_lock_acquire(lock_key: str, ttl: int = 300) -> bool:
    """Try to acquire a Redis SET NX lock. Returns True if acquired."""
    import os

    url = os.getenv("REDIS_URL", "")
    if not url:
        return False
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=5.0)
        return bool(r.set(lock_key, "1", nx=True, ex=ttl))
    except Exception:
        return False


def _redis_lock_release(lock_key: str) -> None:
    """Release a Redis lock."""
    import os

    url = os.getenv("REDIS_URL", "")
    if not url:
        return
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=5.0)
        r.delete(lock_key)
    except Exception:
        pass


def _with_redis_lock(lock_key: str, fn, timeout: int = 300):
    """Execute fn() while holding a Redis-based distributed lock.

    Falls back to running fn() without a lock if Redis is unavailable.
    Polls with exponential backoff up to ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        if _redis_lock_acquire(lock_key, ttl=timeout):
            try:
                return fn()
            finally:
                _redis_lock_release(lock_key)
        if time.monotonic() >= deadline:
            # Lock held too long — run anyway (clone/fetch is idempotent)
            print(f"Redis lock timeout for {lock_key}, proceeding without lock", file=sys.stderr)
            return fn()
        time.sleep(min(delay, deadline - time.monotonic()))
        delay = min(delay * 2, 30)


def ensure_clone(repo_url: str) -> Path:
    """Clone or fetch a repository, flock-protected.

    Returns the path to the repo directory (ready for ``claude --worktree``).
    """
    root = _repos_root()
    key = _repo_key(repo_url)
    key_dir = root / key
    lock_path = key_dir / ".lock"
    rdir = key_dir / "repo"

    # Ensure directories exist
    key_dir.mkdir(parents=True, exist_ok=True)

    def _do_clone_or_fetch():
        token = resolve_github_token()
        with git_askpass_env(token) as extra_env:
            if rdir.exists() and (rdir / ".git").exists():
                _run_git(["git", "fetch", "--all", "--prune"], cwd=rdir, extra_env=extra_env)
            else:
                _run_git(["git", "clone", repo_url, str(rdir)], extra_env=extra_env)
            sanitize_remote_url(str(rdir))

    cfg = get_config()
    if cfg.repos.shared_fs_root:
        # Kubernetes / shared FS: use Redis distributed lock (fcntl doesn't
        # work reliably across NFS mounts from different hosts).
        prefix = f"agenticore:lock:clone:{key}"
        _with_redis_lock(prefix, _do_clone_or_fetch)
    else:
        # Local / Docker: flock is sufficient (single host).
        with open(lock_path, "w") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            try:
                _do_clone_or_fetch()
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)

    return rdir


def _run_git(cmd: list, cwd: Path | None = None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    if result.returncode != 0:
        safe_cmd = " ".join(strip_credentials_from_url(c) for c in cmd)
        safe_stderr = strip_credentials_from_url(result.stderr)
        print(f"git command failed: {safe_cmd}", file=sys.stderr)
        print(f"  stderr: {safe_stderr}", file=sys.stderr)
        raise RuntimeError(f"git failed: {safe_stderr.strip()}")
    return result


def get_default_branch(repo_path: Path) -> str:
    """Detect the default branch (main/master) of a repo."""
    result = _run_git(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=repo_path,
    )
    # Output like "origin/main"
    branch = result.stdout.strip()
    if "/" in branch:
        branch = branch.split("/", 1)[1]
    return branch or "main"
