"""Unit tests for plans module."""

import json
import os
from unittest.mock import patch

import pytest

from agenticore.plans import (
    _plan_name,
    _reset_redis,
    create_plan,
    get_plan,
    list_plans,
    update_plan,
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
def plans_dir(tmp_path, monkeypatch):
    """Use a temp directory for plan files."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr("agenticore.plans._plans_dir", lambda: d)
    return d


@pytest.mark.unit
class TestPlanName:
    def test_slug_from_task(self):
        name = _plan_name("fix the authentication bug in login")
        assert name.startswith("fix-the-authentication-bug-in")
        assert len(name.split("-")) > 5  # slug words + suffix

    def test_slug_strips_special_chars(self):
        name = _plan_name("Fix auth! (urgent) -- NOW")
        assert "!" not in name
        assert "(" not in name
        assert "--" not in name

    def test_slug_has_suffix(self):
        name1 = _plan_name("same task")
        name2 = _plan_name("same task")
        # Each call gets a unique suffix
        assert name1 != name2

    def test_short_task(self):
        name = _plan_name("fix")
        assert name.startswith("fix-")

    def test_empty_task_fallback(self):
        name = _plan_name("")
        assert name.startswith("plan-")


@pytest.mark.unit
class TestCreatePlan:
    def test_creates_plan_with_id(self, plans_dir):
        plan = create_plan(task="fix auth bug")
        assert plan.id
        assert plan.task == "fix auth bug"
        assert plan.status == "pending"
        assert plan.created_at

    def test_persists_to_file(self, plans_dir):
        plan = create_plan(task="add tests")
        path = plans_dir / f"{plan.id}.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["task"] == "add tests"
        assert data["status"] == "pending"

    def test_name_is_set(self, plans_dir):
        plan = create_plan(task="refactor the database layer")
        assert plan.name
        assert "refactor" in plan.name

    def test_optional_fields(self, plans_dir):
        plan = create_plan(
            task="add pagination",
            repo_url="https://github.com/org/repo",
            planning_job_id="job-abc-123",
        )
        assert plan.repo_url == "https://github.com/org/repo"
        assert plan.planning_job_id == "job-abc-123"
        assert plan.execution_job_id == ""
        assert plan.content == ""

    def test_unique_ids(self, plans_dir):
        p1 = create_plan(task="task one")
        p2 = create_plan(task="task two")
        assert p1.id != p2.id


@pytest.mark.unit
class TestGetPlan:
    def test_retrieves_created_plan(self, plans_dir):
        created = create_plan(task="test plan")
        fetched = get_plan(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.task == "test plan"

    def test_returns_none_for_missing(self, plans_dir):
        result = get_plan("nonexistent-id")
        assert result is None

    def test_roundtrip_all_fields(self, plans_dir):
        plan = create_plan(
            task="complex task",
            repo_url="https://github.com/x/y",
            planning_job_id="job-xyz",
        )
        fetched = get_plan(plan.id)
        assert fetched.repo_url == "https://github.com/x/y"
        assert fetched.planning_job_id == "job-xyz"
        assert fetched.name == plan.name


@pytest.mark.unit
class TestUpdatePlan:
    def test_updates_status(self, plans_dir):
        plan = create_plan(task="test")
        updated = update_plan(plan.id, status="ready")
        assert updated.status == "ready"

    def test_updates_content(self, plans_dir):
        plan = create_plan(task="test")
        content = "# Implementation Plan\n\n## Step 1\nDo the thing."
        updated = update_plan(plan.id, content=content, status="ready")
        assert updated.content == content
        assert updated.status == "ready"

    def test_persists_update(self, plans_dir):
        plan = create_plan(task="test")
        update_plan(plan.id, status="ready", execution_job_id="exec-job-1")
        fetched = get_plan(plan.id)
        assert fetched.status == "ready"
        assert fetched.execution_job_id == "exec-job-1"

    def test_returns_none_for_missing(self, plans_dir):
        result = update_plan("nonexistent-id", status="ready")
        assert result is None

    def test_ignores_unknown_fields(self, plans_dir):
        plan = create_plan(task="test")
        # Should not raise, just ignore unknown kwargs
        updated = update_plan(plan.id, nonexistent_field="value")
        assert updated is not None


@pytest.mark.unit
class TestListPlans:
    def test_returns_empty_when_no_plans(self, plans_dir):
        result = list_plans()
        assert result == []

    def test_lists_created_plans(self, plans_dir):
        create_plan(task="first plan")
        create_plan(task="second plan")
        result = list_plans()
        assert len(result) == 2

    def test_sorted_by_created_at_desc(self, plans_dir):
        p1 = create_plan(task="older plan")
        p2 = create_plan(task="newer plan")
        result = list_plans()
        ids = [p.id for p in result]
        # Newer plan should appear first (desc sort)
        assert ids.index(p2.id) < ids.index(p1.id)

    def test_respects_limit(self, plans_dir):
        for i in range(5):
            create_plan(task=f"plan {i}")
        result = list_plans(limit=3)
        assert len(result) == 3

    def test_plan_fields_present(self, plans_dir):
        create_plan(task="check fields", repo_url="https://github.com/x/y")
        result = list_plans()
        assert len(result) == 1
        p = result[0]
        assert p.task == "check fields"
        assert p.repo_url == "https://github.com/x/y"
        assert p.status == "pending"
