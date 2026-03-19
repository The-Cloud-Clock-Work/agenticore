"""Repository cloning and caching with flock-based serialization.

Layout::

    {repos_root}/
    └── {sha256(url)[:12]}/
        ├── .lock
        └── repo/

Clone once, ``git fetch --all`` on re-use. Worktrees are first-class
resources: ``prepare_worktree()`` creates and validates them, then jobs
reference them by ID.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
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

    # 2. Static token
    cfg = get_config()
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

    Returns the path to the repo directory.
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
                # Update local default branch to match origin so new worktrees
                # branch off the latest code (not a stale local checkout).
                try:
                    _run_git(["git", "reset", "--hard", "origin/HEAD"], cwd=rdir)
                except Exception:
                    pass  # non-fatal — worktrees may block checkout
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


def _worktree_dirname(repo_url: str, base_ref: str, wt_id: str) -> str:
    """Build human-readable worktree dir name: {repo}-{branch}-{id_short}."""
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    name = name.removesuffix(".git")
    name = name.replace("_", "-").lower()
    branch = base_ref.replace("_", "-").replace("/", "-").lower()
    return f"{name}-{branch}-{wt_id[:8]}"


# ── Worktree as First-Class Resource ─────────────────────────────────────


@dataclass
class Worktree:
    id: str = ""
    repo_url: str = ""
    base_ref: str = ""
    branch: str = ""
    path: str = ""
    repo_dir: str = ""
    status: str = "preparing"  # preparing|ready|in_use|failed|removed
    created_at: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "Worktree":
        return cls(
            id=data.get("id", ""),
            repo_url=data.get("repo_url", ""),
            base_ref=data.get("base_ref", ""),
            branch=data.get("branch", ""),
            path=data.get("path", ""),
            repo_dir=data.get("repo_dir", ""),
            status=data.get("status", "preparing"),
            created_at=data.get("created_at", ""),
            error=data.get("error", ""),
        )


class WorktreeNotReady(Exception):
    def __init__(self, path: str, attempts: int):
        self.path = path
        self.attempts = attempts
        super().__init__(f"Worktree not ready after {attempts} attempts: {path}")


def _worktrees_meta_dir() -> Path:
    d = Path.home() / ".agenticore" / "worktrees-meta"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_worktree(wt: Worktree) -> None:
    path = _worktrees_meta_dir() / f"{wt.id}.json"
    with open(path, "w") as f:
        json.dump(wt.to_dict(), f, indent=2)


def get_worktree(wt_id: str) -> Optional[Worktree]:
    path = _worktrees_meta_dir() / f"{wt_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return Worktree.from_dict(json.load(f))


def update_worktree(wt_id: str, **kwargs) -> Optional[Worktree]:
    wt = get_worktree(wt_id)
    if wt is None:
        return None
    for key, value in kwargs.items():
        if hasattr(wt, key):
            setattr(wt, key, value)
    _save_worktree(wt)
    return wt


def list_worktrees_from_store(status: str = "") -> list[Worktree]:
    results = []
    meta_dir = _worktrees_meta_dir()
    for p in meta_dir.glob("*.json"):
        try:
            with open(p) as f:
                wt = Worktree.from_dict(json.load(f))
            if status and wt.status != status:
                continue
            results.append(wt)
        except Exception:
            continue
    results.sort(key=lambda w: w.created_at, reverse=True)
    return results


def validate_worktree_ready(wt_path: Path, max_retries: int = 15, delay: float = 1.0) -> None:
    """Readiness gate: verify worktree dir exists, .git resolves, git status OK.

    Busts NFS attribute cache by listing parent dir on each attempt.
    Raises WorktreeNotReady if all retries exhausted.
    """
    for attempt in range(max_retries):
        try:
            os.listdir(wt_path.parent)
        except OSError:
            pass
        if not wt_path.is_dir():
            logger.debug("worktree not visible (attempt %d/%d): %s", attempt + 1, max_retries, wt_path)
            time.sleep(delay)
            continue
        git_link = wt_path / ".git"
        if not git_link.exists():
            logger.debug("worktree .git missing (attempt %d/%d): %s", attempt + 1, max_retries, wt_path)
            time.sleep(delay)
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(wt_path), "status"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return  # READY
        except Exception:
            pass
        time.sleep(delay)
    raise WorktreeNotReady(path=str(wt_path), attempts=max_retries)


def prepare_worktree(repo_url: str, base_ref: str = "main") -> Worktree:
    """Phase 1: Clone repo, create worktree, validate readiness.

    Returns a Worktree with status='ready' on success, status='failed' on error.
    """
    wt_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    wt = Worktree(
        id=wt_id,
        repo_url=repo_url,
        base_ref=base_ref,
        status="preparing",
        created_at=now,
    )
    _save_worktree(wt)

    try:
        rdir = ensure_clone(repo_url)
        wt.repo_dir = str(rdir)

        if not base_ref:
            base_ref = get_default_branch(rdir)
            wt.base_ref = base_ref

        dirname = _worktree_dirname(repo_url, base_ref, wt_id)
        branch = f"agenticore-{wt_id}"
        wt_path = Path.home() / ".agenticore" / "worktrees" / dirname

        wt.branch = branch
        wt.path = str(wt_path)
        _save_worktree(wt)

        _run_git(
            ["git", "worktree", "add", str(wt_path), "-b", branch, f"origin/{base_ref}"],
            cwd=rdir,
        )
        _run_git(
            ["git", "worktree", "lock", str(wt_path), "--reason", f"agenticore: worktree {wt_id}"],
            cwd=rdir,
        )

        validate_worktree_ready(wt_path)

        wt.status = "ready"
        _save_worktree(wt)
        logger.info("Worktree ready: %s at %s", wt_id, wt_path)
        return wt

    except Exception as e:
        wt.status = "failed"
        wt.error = str(e)
        _save_worktree(wt)
        logger.error("Worktree preparation failed: %s — %s", wt_id, e)
        raise


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

        if resp.status_code == 422 and "already exists" in resp.text:
            logger.info("Repo %s/%s already exists", owner, name)
            return

        if resp.status_code == 403:
            last_error = resp.text
            logger.debug("Token lacks repo creation permission, trying next")
            continue

        raise RuntimeError(f"Failed to create repo {owner}/{name}: {resp.status_code} {resp.text}")

    raise RuntimeError(f"Failed to create repo {owner}/{name}: all tokens lack permission. Last: {last_error}")


# ── Worktree Management ───────────────────────────────────────────────────


def list_all_worktrees() -> list[dict]:
    """List all worktrees across all cached repos via `git worktree list`.

    Returns a list of dicts with:
        path, repo_key, branch, created_at, age_hours, size_bytes, pushed
    Sorted by age descending (oldest first).
    """
    root = _repos_root()
    if not root.exists():
        return []

    results = []
    for key_dir in root.iterdir():
        if not key_dir.is_dir():
            continue
        rdir = key_dir / "repo"
        if not (rdir / ".git").exists():
            continue
        for wt in _git_worktree_list(rdir):
            if wt["path"] == str(rdir):
                continue  # skip main checkout
            info = _worktree_info(Path(wt["path"]), rdir, key_dir.name, wt.get("branch", ""))
            if info:
                results.append(info)

    results.sort(key=lambda x: x["age_hours"], reverse=True)
    return results


def _git_worktree_list(rdir: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` output."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=rdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        entries = []
        current = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current = {"path": line[len("worktree ") :]}
            elif line.startswith("branch refs/heads/"):
                current["branch"] = line[len("branch refs/heads/") :]
            elif line == "" and current:
                entries.append(current)
                current = {}
        if current:
            entries.append(current)
        return entries
    except Exception:
        return []


def _worktree_info(wt_dir: Path, rdir: Path, repo_key: str, branch: str) -> Optional[dict]:
    """Gather info about a single worktree directory."""
    try:
        if not wt_dir.exists():
            return None
        stat = wt_dir.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600

        # Size
        try:
            result = subprocess.run(
                ["du", "-sb", str(wt_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            size_bytes = int(result.stdout.split()[0]) if result.returncode == 0 else 0
        except Exception:
            size_bytes = 0

        # Check if branch is pushed to origin
        pushed = False
        if branch:
            try:
                result = subprocess.run(
                    ["git", "branch", "-r", "--list", f"origin/{branch}"],
                    cwd=rdir,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                pushed = bool(result.stdout.strip())
            except Exception:
                pass

        return {
            "path": str(wt_dir),
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


def remove_worktrees(worktree_paths: list[str]) -> list[dict]:
    """Remove specific worktrees by path. Unlocks before removing.

    Also updates store metadata to status='removed'.
    Returns a list of {path, success, error} dicts.
    """
    root = _repos_root()
    results = []

    # Build reverse map: path → store worktree ID for metadata updates
    store_by_path: dict[str, str] = {}
    for wt in list_worktrees_from_store():
        if wt.path:
            store_by_path[wt.path] = wt.id

    for wt_path in worktree_paths:
        wt_dir = Path(wt_path)
        removed = False
        error = None
        for key_dir in root.iterdir():
            if not key_dir.is_dir():
                continue
            rdir = key_dir / "repo"
            if not (rdir / ".git").is_dir():
                continue
            known_paths = {wt["path"] for wt in _git_worktree_list(rdir)}
            if str(wt_dir) not in known_paths:
                continue
            try:
                subprocess.run(
                    ["git", "worktree", "unlock", str(wt_dir)],
                    cwd=rdir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_dir)],
                    cwd=rdir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if wt_dir.exists():
                    import shutil

                    shutil.rmtree(wt_dir)
                removed = True
            except Exception as e:
                error = str(e)
            break
        if not removed and error is None:
            error = "Worktree not found"

        # Update store metadata
        wt_store_id = store_by_path.get(wt_path)
        if wt_store_id and removed:
            update_worktree(wt_store_id, status="removed")

        results.append({"path": wt_path, "success": removed, "error": error})
    return results
