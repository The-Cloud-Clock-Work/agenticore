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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
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


# ── Auto Repo Creation ────────────────────────────────────────────────────


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Parse owner and name from a GitHub URL.

    Supports HTTPS (https://github.com/owner/name[.git]) and
    SSH (git@github.com:owner/name[.git]).

    Returns:
        (owner, name) tuple

    Raises:
        ValueError: If URL is not a recognizable GitHub URL.
    """
    # HTTPS
    m = re.match(r"https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$", repo_url)
    if m:
        return m.group(1), m.group(2)
    # SSH
    m = re.match(r"git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Not a GitHub URL: {repo_url}")


def _repo_exists(repo_url: str) -> bool:
    """Check if a remote repo exists via git ls-remote."""
    token = resolve_github_token()
    with git_askpass_env(token) as extra_env:
        env = os.environ.copy()
        env.update(extra_env)
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", repo_url],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            return result.returncode == 0
        except Exception:
            return False


def ensure_repo_exists(repo_url: str, private: bool = True) -> None:
    """Create a GitHub repo if it doesn't exist.

    No-op if the repo already exists. Always creates with auto_init=True
    so the repo has an initial commit (required for cloning).

    Args:
        repo_url: GitHub HTTPS or SSH URL
        private: Create as private (default: True)

    Raises:
        ValueError: If URL is not a GitHub URL
        RuntimeError: If repo creation fails
    """
    if _repo_exists(repo_url):
        return

    owner, name = _parse_github_repo(repo_url)

    import httpx

    # Try tokens in order: resolve_github_token() first, then static GITHUB_TOKEN fallback.
    # GitHub App tokens may lack administration:write needed for repo creation.
    tokens_to_try = []
    primary = resolve_github_token()
    if primary:
        tokens_to_try.append(primary)
    static = os.getenv("GITHUB_TOKEN", "")
    if static and static != primary:
        tokens_to_try.append(static)

    if not tokens_to_try:
        raise RuntimeError("No GitHub token available — cannot create repo")

    payload = {"name": name, "private": private, "auto_init": True}
    last_error = ""

    for token in tokens_to_try:
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

        resp = httpx.post(
            f"https://api.github.com/orgs/{owner}/repos",
            json=payload,
            headers=headers,
            timeout=30,
        )

        if resp.status_code == 404:
            # Not an org — try user endpoint
            resp = httpx.post(
                "https://api.github.com/user/repos",
                json=payload,
                headers=headers,
                timeout=30,
            )

        if resp.status_code in (201, 200):
            logger.info("Created repo %s/%s (private=%s)", owner, name, private)
            return

        if resp.status_code == 403:
            last_error = resp.text
            logger.debug("Token lacks repo creation permission, trying next")
            continue

        raise RuntimeError(f"Failed to create repo {owner}/{name}: {resp.status_code} {resp.text}")

    raise RuntimeError(f"Failed to create repo {owner}/{name}: all tokens lack permission. Last: {last_error}")


# ── Worktree Management ───────────────────────────────────────────────────


def list_all_worktrees() -> list[dict]:
    """List all worktrees across all cached repos.

    Returns a list of dicts with:
        job_id, repo_key, branch, created_at, age_hours, size_bytes, pushed
    Sorted by age descending (oldest first).
    """
    root = _repos_root()
    if not root.exists():
        return []

    results = []
    for key_dir in root.iterdir():
        if not key_dir.is_dir():
            continue
        wt_root = key_dir / "worktrees"
        if not wt_root.exists():
            continue
        rdir = key_dir / "repo"
        for wt_dir in wt_root.iterdir():
            if not wt_dir.is_dir():
                continue
            job_id = wt_dir.name
            info = _worktree_info(wt_dir, rdir, key_dir.name, job_id)
            if info:
                results.append(info)

    results.sort(key=lambda x: x["age_hours"], reverse=True)
    return results


def _worktree_info(wt_dir: Path, rdir: Path, repo_key: str, job_id: str) -> Optional[dict]:
    """Gather info about a single worktree directory."""
    try:
        stat = wt_dir.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600

        # Read branch from HEAD
        branch = ""
        head_file = wt_dir / ".git" / "HEAD" if (wt_dir / ".git" / "HEAD").exists() else wt_dir / "HEAD"
        # For worktrees, .git is a file pointing to the main repo's worktree metadata
        # The branch is in the main repo's worktrees/<name>/HEAD
        wt_meta = rdir / ".git" / "worktrees" / job_id / "HEAD"
        if wt_meta.exists():
            head_content = wt_meta.read_text().strip()
            if head_content.startswith("ref: refs/heads/"):
                branch = head_content[len("ref: refs/heads/"):]
        elif (wt_dir / ".git").exists() and (wt_dir / ".git").is_file():
            # Worktree .git is a file — try reading HEAD from the checkout
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=wt_dir, capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    branch = result.stdout.strip()
            except Exception:
                pass

        # Size
        try:
            result = subprocess.run(
                ["du", "-sb", str(wt_dir)],
                capture_output=True, text=True, timeout=10,
            )
            size_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else 0
        except Exception:
            size_bytes = 0

        # Check if branch is pushed to origin
        pushed = False
        if branch and rdir.exists():
            try:
                result = subprocess.run(
                    ["git", "branch", "-r", "--list", f"origin/{branch}"],
                    cwd=rdir, capture_output=True, text=True, timeout=5,
                )
                pushed = bool(result.stdout.strip())
            except Exception:
                pass

        return {
            "job_id": job_id,
            "repo_key": repo_key,
            "branch": branch,
            "created_at": mtime.isoformat(),
            "age_hours": round(age_hours, 1),
            "size_bytes": size_bytes,
            "pushed": pushed,
        }
    except Exception as e:
        logger.warning("Failed to read worktree info for %s: %s", wt_dir, e)
        return None


def remove_worktrees(job_ids: list[str]) -> list[dict]:
    """Remove specific worktrees by job ID.

    Returns a list of {job_id, success, error} dicts.
    """
    root = _repos_root()
    results = []
    for job_id in job_ids:
        removed = False
        error = None
        # Find the worktree across all repo keys
        for key_dir in root.iterdir():
            if not key_dir.is_dir():
                continue
            wt_dir = key_dir / "worktrees" / job_id
            if not wt_dir.exists():
                continue
            rdir = key_dir / "repo"
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_dir)],
                    cwd=rdir, capture_output=True, text=True, timeout=30,
                )
                # If git worktree remove didn't clean up, force delete
                if wt_dir.exists():
                    import shutil
                    shutil.rmtree(wt_dir)
                removed = True
            except Exception as e:
                error = str(e)
            break
        if not removed and error is None:
            error = "Worktree not found"
        results.append({"job_id": job_id, "success": removed, "error": error})
    return results
