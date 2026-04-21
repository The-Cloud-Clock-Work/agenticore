"""Unit tests for pr.py — auto-PR creation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenticore.jobs import Job
from agenticore.pr import _commit_untracked, create_auto_pr

# ---------------------------------------------------------------------------
# Token guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateAutoPrTokenGuard:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_token(self):
        """create_auto_pr returns None when resolve_github_token() returns None."""
        job = Job(
            id="job-1",
            repo_url="https://github.com/org/repo",
            task="test",
            profile="code",
            status="succeeded",
        )
        with patch("agenticore.pr.resolve_github_token", return_value=None):
            result = await create_auto_pr(job)
        assert result is None


def _make_job(**kwargs) -> Job:
    defaults = dict(
        id="job-123",
        repo_url="https://github.com/org/repo",
        task="write a health check script",
        profile="code",
        status="succeeded",
        worktree_path="",
    )
    defaults.update(kwargs)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# _commit_untracked
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommitUntracked:
    @pytest.mark.asyncio
    async def test_skips_nonexistent_path(self, tmp_path):
        """No subprocess calls when the worktree directory doesn't exist."""
        missing = tmp_path / "no-such-dir"
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await _commit_untracked(missing, "job-1")
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_stages_and_commits(self, tmp_path):
        """git add -A then git commit are run in the worktree directory."""
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _commit_untracked(tmp_path, "job-42")

        assert calls[0][:2] == ("git", "add")
        assert "-A" in calls[0]
        assert calls[1][:2] == ("git", "commit")
        assert "job-42" in calls[1][calls[1].index("-m") + 1]

    @pytest.mark.asyncio
    async def test_nothing_to_commit_is_tolerated(self, tmp_path):
        """git commit rc=1 (nothing to commit) does not raise."""

        async def fake_exec(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b"nothing to commit"))
            proc.returncode = 1
            return proc

        # Should not raise
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _commit_untracked(tmp_path, "job-99")

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self, tmp_path):
        """Subprocess errors are caught and don't propagate."""
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("git not found")):
            await _commit_untracked(tmp_path, "job-err")  # must not raise


# ---------------------------------------------------------------------------
# create_auto_pr — commit_untracked integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateAutoPrCommitsUntracked:
    @pytest.mark.asyncio
    async def test_commit_untracked_called_when_worktree_path_set(self, tmp_path):
        """create_auto_pr calls _commit_untracked before checking for changes."""
        job = _make_job(worktree_path=str(tmp_path))

        with (
            patch("agenticore.pr.resolve_github_token", return_value="ghp_test"),
            patch("agenticore.pr.repo_dir") as mock_repo_dir,
            patch("agenticore.pr._commit_untracked", new_callable=AsyncMock) as mock_commit,
            patch("agenticore.pr._get_worktree_branch", new_callable=AsyncMock, return_value="cc-abc"),
            patch("agenticore.pr._has_changes", new_callable=AsyncMock, return_value=False),
        ):
            mock_repo_dir.return_value = tmp_path
            await create_auto_pr(job)

        mock_commit.assert_awaited_once_with(Path(str(tmp_path)), "job-123")

    @pytest.mark.asyncio
    async def test_commit_untracked_skipped_when_no_worktree_path(self, tmp_path):
        """create_auto_pr skips _commit_untracked when worktree_path is empty."""
        job = _make_job(worktree_path="")

        with (
            patch("agenticore.pr.resolve_github_token", return_value="ghp_test"),
            patch("agenticore.pr.repo_dir") as mock_repo_dir,
            patch("agenticore.pr._commit_untracked", new_callable=AsyncMock) as mock_commit,
            patch("agenticore.pr._get_worktree_branch", new_callable=AsyncMock, return_value="cc-abc"),
            patch("agenticore.pr._has_changes", new_callable=AsyncMock, return_value=False),
        ):
            mock_repo_dir.return_value = tmp_path
            await create_auto_pr(job)

        mock_commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pr_created_after_commit(self, tmp_path):
        """Full happy path: untracked files committed, PR created."""
        job = _make_job(worktree_path=str(tmp_path))

        with (
            patch("agenticore.pr.resolve_github_token", return_value="ghp_test"),
            patch("agenticore.pr.repo_dir") as mock_repo_dir,
            patch("agenticore.pr._commit_untracked", new_callable=AsyncMock),
            patch("agenticore.pr._get_worktree_branch", new_callable=AsyncMock, return_value="cc-abc"),
            patch("agenticore.pr._has_changes", new_callable=AsyncMock, return_value=True),
            patch("agenticore.pr._push_branch", new_callable=AsyncMock, return_value=True),
            patch(
                "agenticore.pr._create_pr", new_callable=AsyncMock, return_value="https://github.com/org/repo/pull/1"
            ),
        ):
            mock_repo_dir.return_value = tmp_path
            pr_url = await create_auto_pr(job)

        assert pr_url == "https://github.com/org/repo/pull/1"
