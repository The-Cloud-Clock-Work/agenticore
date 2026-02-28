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
import subprocess
import sys
import time
from pathlib import Path

from agenticore.config import get_config


def _repo_key(repo_url: str) -> str:
    """Deterministic short key for a repo URL."""
    return hashlib.sha256(repo_url.encode()).hexdigest()[:12]


def _repos_root() -> Path:
    cfg = get_config()
    return Path(cfg.repos.root)


def repo_dir(repo_url: str) -> Path:
    """Return the path to the cloned repo directory."""
    return _repos_root() / _repo_key(repo_url) / "repo"


def _authenticated_url(repo_url: str) -> str:
    """Inject GITHUB_TOKEN into HTTPS clone URLs for private repo access."""
    cfg = get_config()
    token = cfg.github.token
    if token and repo_url.startswith("https://github.com/"):
        return repo_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
    return repo_url


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
        if rdir.exists() and (rdir / ".git").exists():
            _run_git(["git", "fetch", "--all", "--prune"], cwd=rdir)
        else:
            clone_url = _authenticated_url(repo_url)
            _run_git(["git", "clone", clone_url, str(rdir)])

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


def _run_git(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        print(f"git command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  stderr: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"git failed: {result.stderr.strip()}")
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
