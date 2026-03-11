"""Auto-PR creation after successful job execution.

When ``profile.auto_pr: true`` and the job succeeds (exit_code == 0):
1. Check if there are changes in the worktree branch
2. Push the branch
3. Create PR via ``gh pr create``
4. Return PR URL
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from agenticore.git_credentials import git_askpass_env
from agenticore.jobs import Job
from agenticore.repos import repo_dir, resolve_github_token

logger = logging.getLogger(__name__)


async def create_auto_pr(job: Job) -> Optional[str]:
    """Create a PR for a completed job.

    Requires:
    - A GitHub token (App, Auth Broker, or static GITHUB_TOKEN)
    - Changes committed by Claude in the worktree branch

    Returns:
        PR URL string, or None if no changes or PR creation failed.
    """
    if not job.repo_url:
        return None

    token = resolve_github_token()
    if not token:
        logger.warning("No GitHub token available — skipping auto-PR for job %s", job.id)
        return None

    rdir = repo_dir(job.repo_url)
    if not rdir.exists():
        return None

    # Deterministic branch name from bespoke worktree
    branch = f"agenticore-{job.id[:8]}"
    if not await _branch_exists(rdir, branch):
        # Fallback for legacy jobs where Claude picked its own branch name
        branch = await _get_worktree_branch(rdir, job.id)
    if not branch:
        return None

    # Stage and commit any files Claude wrote but didn't git-add.
    # Claude Code's --worktree mode leaves files untracked when the task
    # doesn't include explicit git operations.
    if job.worktree_path:
        await _commit_untracked(Path(job.worktree_path), job.id)

    # Check if there are commits on this branch
    has_changes = await _has_changes(rdir, branch)
    if not has_changes:
        return None

    # Push the branch
    push_ok = await _push_branch(rdir, branch)
    if not push_ok:
        return None

    # Create PR
    pr_url = await _create_pr(rdir, branch, job)
    return pr_url


async def _commit_untracked(worktree_path: Path, job_id: str) -> None:
    """Stage and commit any untracked/modified files left by Claude.

    Claude Code's --worktree mode may write files without running git.
    This ensures those changes are committed onto the branch before the
    PR is created.  A non-zero exit from ``git commit`` (e.g. "nothing
    to commit") is silently ignored.
    """
    if not worktree_path.exists():
        return
    try:
        add = await asyncio.create_subprocess_exec(
            "git",
            "add",
            "-A",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await add.communicate()

        commit = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            f"chore: apply changes from agenticore job {job_id}",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await commit.communicate()
        # rc=1 with "nothing to commit" is normal when Claude committed already
    except Exception as e:
        print(f"_commit_untracked failed for {worktree_path}: {e}", file=sys.stderr)


async def _branch_exists(rdir, branch: str) -> bool:
    """Check if a local branch exists."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--verify",
            branch,
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False


async def _get_worktree_branch(rdir, _job_id: str) -> Optional[str]:
    """Find the most recently created worktree branch for this job.

    Claude Code creates worktree branches with varying prefixes (cc-*, worktree-*).
    We sort by committerdate descending and return the newest non-main branch.
    """
    try:
        result = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        skip = {"main", "master", "dev"}
        for line in stdout.decode().strip().split("\n"):
            b = line.strip()
            if b and b not in skip:
                return b
        return None
    except Exception:
        return None


async def _has_changes(rdir, branch: str) -> bool:
    """Check if the branch has commits ahead of the default branch."""
    try:
        result = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"origin/HEAD..{branch}",
            "--oneline",
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        return bool(stdout.decode().strip())
    except Exception:
        return False


async def _push_branch(rdir, branch: str) -> bool:
    """Push the branch to origin using GIT_ASKPASS for credentials."""
    token = resolve_github_token()
    with git_askpass_env(token) as extra_env:
        try:
            env = os.environ.copy()
            env.update(extra_env)
            result = await asyncio.create_subprocess_exec(
                "git",
                "push",
                "origin",
                branch,
                cwd=rdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            return result.returncode == 0
        except Exception as e:
            print(f"Push failed: {e}", file=sys.stderr)
            return False


async def _create_pr(rdir, branch: str, job: Job) -> Optional[str]:
    """Create a PR using the GitHub REST API.

    Uses REST instead of ``gh`` CLI because GitHub App installation tokens
    fail GraphQL repo resolution in ``gh pr create``.
    """
    import re

    import httpx

    title = job.task[:70] if len(job.task) > 70 else job.task
    body = f"Job: {job.id}\n\nTask: {job.task}\n\nProfile: {job.profile}"

    token = resolve_github_token()
    if not token:
        print("PR creation skipped: no GitHub token", file=sys.stderr)
        return None

    # Extract owner/repo from the remote URL
    try:
        result = await asyncio.create_subprocess_exec(
            "git",
            "remote",
            "get-url",
            "origin",
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        remote_url = stdout.decode().strip()
    except Exception:
        return None

    # Parse owner/repo from HTTPS or SSH URL
    m = re.match(r"https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$", remote_url)
    if not m:
        m = re.match(r"git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$", remote_url)
    if not m:
        print(f"PR creation failed: cannot parse remote URL: {remote_url}", file=sys.stderr)
        return None

    owner, repo_name = m.group(1), m.group(2)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                json={"title": title, "head": branch, "base": "main", "body": body},
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.status_code == 201:
            return resp.json().get("html_url", "")
        else:
            print(f"PR creation failed: HTTP {resp.status_code} — {resp.text[:200]}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"PR creation error: {e}", file=sys.stderr)
        return None
