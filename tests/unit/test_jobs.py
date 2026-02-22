"""Unit tests for jobs module."""

import json
import os
from unittest.mock import patch

import pytest

from agenticore.jobs import (
    Job,
    _reset_redis,
    cancel_job,
    create_job,
    get_job,
    list_jobs,
    update_job,
)


@pytest.fixture(autouse=True)
def _no_redis():
    """Force file fallback by ensuring no REDIS_URL."""
    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
        _reset_redis()
        yield
    _reset_redis()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    """Use a temp directory for job files."""
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr("agenticore.jobs._jobs_dir", lambda: d)
    return d


@pytest.mark.unit
class TestCreateJob:
    def test_creates_job_with_id(self, jobs_dir):
        job = create_job(task="fix bug", profile="code")
        assert job.id
        assert job.task == "fix bug"
        assert job.profile == "code"
        assert job.status == "queued"
        assert job.created_at

    def test_persists_to_file(self, jobs_dir):
        job = create_job(task="test")
        path = jobs_dir / f"{job.id}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["task"] == "test"
        assert data["status"] == "queued"

    def test_custom_fields(self, jobs_dir):
        job = create_job(
            task="deploy",
            profile="review",
            repo_url="https://github.com/org/repo",
            base_ref="develop",
            mode="sync",
            session_id="abc-123",
        )
        assert job.profile == "review"
        assert job.repo_url == "https://github.com/org/repo"
        assert job.base_ref == "develop"
        assert job.mode == "sync"
        assert job.session_id == "abc-123"


@pytest.mark.unit
class TestGetJob:
    def test_retrieves_created_job(self, jobs_dir):
        created = create_job(task="test")
        fetched = get_job(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.task == "test"

    def test_returns_none_for_missing(self, jobs_dir):
        assert get_job("nonexistent-id") is None


@pytest.mark.unit
class TestUpdateJob:
    def test_updates_status(self, jobs_dir):
        job = create_job(task="test")
        updated = update_job(job.id, status="running", started_at="2024-01-01T00:00:00Z")
        assert updated.status == "running"
        assert updated.started_at == "2024-01-01T00:00:00Z"

        # Verify persisted
        fetched = get_job(job.id)
        assert fetched.status == "running"

    def test_updates_exit_code(self, jobs_dir):
        job = create_job(task="test")
        updated = update_job(job.id, status="succeeded", exit_code=0)
        assert updated.exit_code == 0

    def test_returns_none_for_missing(self, jobs_dir):
        assert update_job("missing", status="running") is None


@pytest.mark.unit
class TestListJobs:
    def test_lists_all_jobs(self, jobs_dir):
        create_job(task="job1")
        create_job(task="job2")
        create_job(task="job3")
        jobs = list_jobs()
        assert len(jobs) == 3

    def test_filter_by_status(self, jobs_dir):
        j1 = create_job(task="job1")
        j2 = create_job(task="job2")
        update_job(j1.id, status="running")

        running = list_jobs(status="running")
        assert len(running) == 1
        assert running[0].id == j1.id

        queued = list_jobs(status="queued")
        assert len(queued) == 1
        assert queued[0].id == j2.id

    def test_limit(self, jobs_dir):
        for i in range(10):
            create_job(task=f"job{i}")
        jobs = list_jobs(limit=5)
        assert len(jobs) == 5

    def test_sorted_by_created_at_desc(self, jobs_dir):
        create_job(task="first")
        create_job(task="second")
        jobs = list_jobs()
        # Most recent first
        assert jobs[0].created_at >= jobs[1].created_at


@pytest.mark.unit
class TestCancelJob:
    def test_cancels_queued_job(self, jobs_dir):
        job = create_job(task="test")
        cancelled = cancel_job(job.id)
        assert cancelled.status == "cancelled"
        assert cancelled.ended_at is not None

    def test_cancel_already_succeeded(self, jobs_dir):
        job = create_job(task="test")
        update_job(job.id, status="succeeded")
        result = cancel_job(job.id)
        assert result.status == "succeeded"  # No change

    def test_cancel_missing_returns_none(self, jobs_dir):
        assert cancel_job("missing") is None


@pytest.mark.unit
class TestJobSerialization:
    def test_to_dict(self):
        job = Job(id="test-id", task="fix bug", status="queued", created_at="2024-01-01")
        d = job.to_dict()
        assert d["id"] == "test-id"
        assert d["task"] == "fix bug"
        # None values excluded
        assert "pr_url" not in d

    def test_from_dict(self):
        data = {"id": "test", "task": "deploy", "status": "running", "exit_code": 0}
        job = Job.from_dict(data)
        assert job.id == "test"
        assert job.task == "deploy"
        assert job.exit_code == 0

    def test_roundtrip(self):
        original = Job(id="rt", task="test", status="queued", created_at="now", repo_url="https://x.com")
        d = original.to_dict()
        restored = Job.from_dict(d)
        assert restored.id == original.id
        assert restored.task == original.task
        assert restored.repo_url == original.repo_url
