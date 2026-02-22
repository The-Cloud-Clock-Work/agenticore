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

    # flock serialization — only one clone/fetch at a time per repo
    with open(lock_path, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            if rdir.exists() and (rdir / ".git").exists():
                # Repo exists — fetch latest
                _run_git(["git", "fetch", "--all", "--prune"], cwd=rdir)
            else:
                # Fresh clone
                _run_git(["git", "clone", repo_url, str(rdir)])
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
