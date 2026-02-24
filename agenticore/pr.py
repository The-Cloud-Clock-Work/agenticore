"""Auto-PR creation after successful job execution.

When ``profile.auto_pr: true`` and the job succeeds (exit_code == 0):
1. Check if there are changes in the worktree branch
2. Push the branch
3. Create PR via ``gh pr create``
4. Return PR URL
"""

import asyncio
import sys
from typing import Optional

from agenticore.jobs import Job
from agenticore.repos import repo_dir


async def create_auto_pr(job: Job) -> Optional[str]:
    """Create a PR for a completed job.

    Requires:
    - GITHUB_TOKEN in env (for ``gh`` CLI)
    - Changes committed by Claude in the worktree branch

    Returns:
        PR URL string, or None if no changes or PR creation failed.
    """
    if not job.repo_url:
        return None

    rdir = repo_dir(job.repo_url)
    if not rdir.exists():
        return None

    # Find the worktree branch created by Claude
    # Claude --worktree creates branches like cc-worktree-{hash}
    branch = await _get_worktree_branch(rdir, job.id)
    if not branch:
        return None

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


async def _get_worktree_branch(rdir, _job_id: str) -> Optional[str]:
    """Find the worktree branch for this job."""
    try:
        result = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--list",
            "cc-*",
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        branches = [b.strip().lstrip("* ") for b in stdout.decode().strip().split("\n") if b.strip()]
        # Return the most recent cc- branch (there should typically be one)
        return branches[-1] if branches else None
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
    """Push the branch to origin."""
    try:
        result = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "origin",
            branch,
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await result.communicate()
        return result.returncode == 0
    except Exception as e:
        print(f"Push failed: {e}", file=sys.stderr)
        return False


async def _create_pr(rdir, branch: str, job: Job) -> Optional[str]:
    """Create a PR using the gh CLI."""
    title = job.task[:70] if len(job.task) > 70 else job.task
    body = f"Job: {job.id}\n\nTask: {job.task}\n\nProfile: {job.profile}"

    try:
        result = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            cwd=rdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await result.communicate()
        if result.returncode == 0:
            return stdout.decode().strip()
        else:
            print(f"PR creation failed: {stderr.decode()}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"PR creation error: {e}", file=sys.stderr)
        return None
