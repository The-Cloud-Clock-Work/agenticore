"""Unit tests for CLI module.

Tests every subcommand, flag, argument parsing, output formatting,
error handling, and the arg→API translation layer.
"""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agenticore import __version__
from agenticore.cli import (
    _api_url,
    _cmd_cancel,
    _cmd_drain,
    _cmd_init_shared_fs,
    _cmd_job,
    _cmd_jobs,
    _cmd_profiles,
    _cmd_run,
    _cmd_serve,
    _cmd_status,
    _cmd_update,
    _cmd_version,
    _get_installed_version,
    main,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse(*argv):
    """Run main()'s argparse on the given argv and return parsed Namespace."""
    import argparse

    # Re-create the parser (mirrors main())
    parser = argparse.ArgumentParser(prog="agenticore")
    parser.add_argument("--version", action="version", version=f"agenticore {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run")
    p_run.add_argument("task")
    p_run.add_argument("--repo", "-r")
    p_run.add_argument("--profile", "-p")
    p_run.add_argument("--base-ref", default="main")
    p_run.add_argument("--wait", "-w", action="store_true")
    p_run.add_argument("--session-id")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--host")

    p_jobs = sub.add_parser("jobs")
    p_jobs.add_argument("--limit", "-n", type=int, default=20)
    p_jobs.add_argument("--status", "-s")

    p_job = sub.add_parser("job")
    p_job.add_argument("job_id")
    p_job.add_argument("--json", action="store_true")

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("job_id")

    sub.add_parser("profiles")
    sub.add_parser("status")

    p_update = sub.add_parser("update")
    p_update.add_argument("--source")

    sub.add_parser("version")

    return parser.parse_args(list(argv))


# ── Argument Parsing ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestArgParsing:
    """Verify that argparse maps argv to correct command + args."""

    def test_run_is_task_submission(self):
        args = _parse("run", "fix the bug")
        assert args.command == "run"
        assert args.task == "fix the bug"

    def test_run_default_no_wait(self):
        """run is async (fire and forget) by default."""
        args = _parse("run", "fix bug")
        assert args.wait is False

    def test_run_wait_flag(self):
        args = _parse("run", "fix bug", "--wait")
        assert args.wait is True

    def test_run_wait_short_flag(self):
        args = _parse("run", "fix bug", "-w")
        assert args.wait is True

    def test_run_repo_flag(self):
        args = _parse("run", "task", "--repo", "https://github.com/org/repo")
        assert args.repo == "https://github.com/org/repo"

    def test_run_repo_short_flag(self):
        args = _parse("run", "task", "-r", "https://github.com/org/repo")
        assert args.repo == "https://github.com/org/repo"

    def test_run_profile_flag(self):
        args = _parse("run", "task", "--profile", "review")
        assert args.profile == "review"

    def test_run_profile_short_flag(self):
        args = _parse("run", "task", "-p", "review")
        assert args.profile == "review"

    def test_run_base_ref_default(self):
        args = _parse("run", "task")
        assert args.base_ref == "main"

    def test_run_base_ref_custom(self):
        args = _parse("run", "task", "--base-ref", "develop")
        assert args.base_ref == "develop"

    def test_run_session_id(self):
        args = _parse("run", "task", "--session-id", "sess-abc")
        assert args.session_id == "sess-abc"

    def test_run_all_flags_combined(self):
        args = _parse(
            "run",
            "do it",
            "-r",
            "https://github.com/org/repo",
            "-p",
            "code",
            "--base-ref",
            "staging",
            "--wait",
            "--session-id",
            "s123",
        )
        assert args.task == "do it"
        assert args.repo == "https://github.com/org/repo"
        assert args.profile == "code"
        assert args.base_ref == "staging"
        assert args.wait is True
        assert args.session_id == "s123"

    def test_serve_command(self):
        args = _parse("serve")
        assert args.command == "serve"

    def test_serve_port_flag(self):
        args = _parse("serve", "--port", "9000")
        assert args.port == 9000

    def test_serve_host_flag(self):
        args = _parse("serve", "--host", "0.0.0.0")
        assert args.host == "0.0.0.0"

    def test_jobs_command(self):
        args = _parse("jobs")
        assert args.command == "jobs"
        assert args.limit == 20  # default

    def test_jobs_limit_flag(self):
        args = _parse("jobs", "--limit", "5")
        assert args.limit == 5

    def test_jobs_limit_short_flag(self):
        args = _parse("jobs", "-n", "10")
        assert args.limit == 10

    def test_jobs_status_filter(self):
        args = _parse("jobs", "--status", "running")
        assert args.status == "running"

    def test_jobs_status_short_flag(self):
        args = _parse("jobs", "-s", "queued")
        assert args.status == "queued"

    def test_job_command(self):
        args = _parse("job", "abc-123")
        assert args.command == "job"
        assert args.job_id == "abc-123"

    def test_job_json_flag(self):
        args = _parse("job", "abc-123", "--json")
        assert args.json is True

    def test_job_json_default_off(self):
        args = _parse("job", "abc-123")
        assert args.json is False

    def test_cancel_command(self):
        args = _parse("cancel", "abc-123")
        assert args.command == "cancel"
        assert args.job_id == "abc-123"

    def test_profiles_command(self):
        args = _parse("profiles")
        assert args.command == "profiles"

    def test_status_command(self):
        args = _parse("status")
        assert args.command == "status"

    def test_version_command(self):
        args = _parse("version")
        assert args.command == "version"

    def test_update_command(self):
        args = _parse("update")
        assert args.command == "update"

    def test_update_source_flag(self):
        args = _parse("update", "--source", "git+https://github.com/org/agenticore.git")
        assert args.source == "git+https://github.com/org/agenticore.git"

    def test_no_submit_command(self):
        """The old 'submit' command no longer exists."""
        with pytest.raises(SystemExit):
            _parse("submit", "task")


# ── API URL ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestApiUrl:
    def test_default_url(self):
        with patch.dict("os.environ", {"AGENTICORE_HOST": "", "AGENTICORE_PORT": ""}, clear=False):
            # Defaults when env vars are empty
            url = _api_url()
            assert "http://" in url

    @patch.dict("os.environ", {"AGENTICORE_HOST": "10.0.0.1", "AGENTICORE_PORT": "9000"})
    def test_custom_host_port(self):
        assert _api_url() == "http://10.0.0.1:9000"

    @patch.dict("os.environ", {"AGENTICORE_HOST": "localhost", "AGENTICORE_PORT": "8200"})
    def test_standard_url(self):
        assert _api_url() == "http://localhost:8200"


# ── run command (task submission) ─────────────────────────────────────────


@pytest.mark.unit
class TestCmdRun:
    """Tests for _cmd_run — the task submission handler."""

    @patch("agenticore.cli._api_post")
    def test_async_submit_success(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {
                "id": "job-001",
                "status": "queued",
                "profile": "code",
            },
        }
        args = SimpleNamespace(
            task="fix the bug",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id=None,
        )
        _cmd_run(args)

        # Verify API call
        mock_post.assert_called_once_with(
            "/jobs",
            {"task": "fix the bug", "repo_url": "", "profile": "", "base_ref": "main", "wait": False},
        )

        out = capsys.readouterr().out
        assert "job-001" in out
        assert "queued" in out

    @patch("agenticore.cli._api_post")
    def test_submit_with_all_options(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {
                "id": "job-002",
                "status": "queued",
                "profile": "review",
                "repo_url": "https://github.com/org/repo",
            },
        }
        args = SimpleNamespace(
            task="add tests",
            repo="https://github.com/org/repo",
            profile="review",
            base_ref="develop",
            wait=False,
            session_id="sess-1",
        )
        _cmd_run(args)

        payload = mock_post.call_args[0][1]
        assert payload["task"] == "add tests"
        assert payload["repo_url"] == "https://github.com/org/repo"
        assert payload["profile"] == "review"
        assert payload["base_ref"] == "develop"
        assert payload["wait"] is False
        assert payload["session_id"] == "sess-1"

        out = capsys.readouterr().out
        assert "Repo:" in out

    @patch("agenticore.cli._api_post")
    def test_wait_flag_sent_in_payload(self, mock_post, capsys):
        """--wait flag is forwarded to the API."""
        mock_post.return_value = {
            "success": True,
            "job": {"id": "job-003", "status": "succeeded", "profile": "code", "output": "Done!"},
        }
        args = SimpleNamespace(
            task="deploy",
            repo=None,
            profile=None,
            base_ref="main",
            wait=True,
            session_id=None,
        )
        _cmd_run(args)

        payload = mock_post.call_args[0][1]
        assert payload["wait"] is True

        out = capsys.readouterr().out
        assert "Done!" in out

    @patch("agenticore.cli._api_post")
    def test_wait_no_output(self, mock_post, capsys):
        """--wait with no output in response doesn't crash."""
        mock_post.return_value = {
            "success": True,
            "job": {"id": "job-004", "status": "succeeded", "profile": "code"},
        }
        args = SimpleNamespace(
            task="task",
            repo=None,
            profile=None,
            base_ref="main",
            wait=True,
            session_id=None,
        )
        _cmd_run(args)
        out = capsys.readouterr().out
        assert "job-004" in out

    @patch("agenticore.cli._api_post")
    def test_submit_api_error(self, mock_post, capsys):
        mock_post.return_value = {"success": False, "error": "bad request"}
        args = SimpleNamespace(
            task="task",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_run(args)
        assert exc_info.value.code == 1
        assert "bad request" in capsys.readouterr().err

    @patch("agenticore.cli._api_post", side_effect=ConnectionError("refused"))
    def test_submit_connection_error(self, mock_post, capsys):
        args = SimpleNamespace(
            task="task",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_run(args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "refused" in err
        assert "server running" in err.lower()

    @patch("agenticore.cli._api_post")
    def test_session_id_omitted_when_none(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {"id": "job-005", "status": "queued", "profile": "code"},
        }
        args = SimpleNamespace(
            task="task",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id=None,
        )
        _cmd_run(args)
        payload = mock_post.call_args[0][1]
        assert "session_id" not in payload

    @patch("agenticore.cli._api_post")
    def test_session_id_included_when_set(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {"id": "job-006", "status": "queued", "profile": "code"},
        }
        args = SimpleNamespace(
            task="task",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id="s-abc",
        )
        _cmd_run(args)
        payload = mock_post.call_args[0][1]
        assert payload["session_id"] == "s-abc"

    @patch("agenticore.cli._api_post")
    def test_repo_none_becomes_empty_string(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {"id": "j", "status": "queued", "profile": "code"},
        }
        args = SimpleNamespace(
            task="t",
            repo=None,
            profile=None,
            base_ref="main",
            wait=False,
            session_id=None,
        )
        _cmd_run(args)
        payload = mock_post.call_args[0][1]
        assert payload["repo_url"] == ""
        assert payload["profile"] == ""


# ── serve command ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdServe:
    def test_serve_sets_port_env(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch("agenticore.server.main") as mock_main:
                args = SimpleNamespace(port=9000, host=None)
                _cmd_serve(args)
                import os

                assert os.environ["AGENTICORE_PORT"] == "9000"
                mock_main.assert_called_once()

    def test_serve_sets_host_env(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch("agenticore.server.main") as mock_main:
                args = SimpleNamespace(port=None, host="0.0.0.0")
                _cmd_serve(args)
                import os

                assert os.environ["AGENTICORE_HOST"] == "0.0.0.0"
                mock_main.assert_called_once()

    def test_serve_no_args(self):
        with patch("agenticore.server.main") as mock_main:
            args = SimpleNamespace(port=None, host=None)
            _cmd_serve(args)
            mock_main.assert_called_once()


# ── jobs command ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdJobs:
    @patch("agenticore.cli._api_get")
    def test_lists_jobs_table(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "jobs": [
                {"id": "j1", "status": "queued", "profile": "code", "task": "fix bug"},
                {"id": "j2", "status": "running", "profile": "review", "task": "add tests"},
            ],
        }
        args = SimpleNamespace(limit=20, status=None)
        _cmd_jobs(args)

        mock_get.assert_called_once_with("/jobs?limit=20")
        out = capsys.readouterr().out
        assert "j1" in out
        assert "j2" in out
        assert "fix bug" in out
        assert "ID" in out  # table header

    @patch("agenticore.cli._api_get")
    def test_jobs_with_status_filter(self, mock_get, capsys):
        mock_get.return_value = {"success": True, "jobs": []}
        args = SimpleNamespace(limit=10, status="running")
        _cmd_jobs(args)
        mock_get.assert_called_once_with("/jobs?limit=10&status=running")

    @patch("agenticore.cli._api_get")
    def test_jobs_empty_list(self, mock_get, capsys):
        mock_get.return_value = {"success": True, "jobs": []}
        args = SimpleNamespace(limit=20, status=None)
        _cmd_jobs(args)
        out = capsys.readouterr().out
        assert "No jobs found" in out

    @patch("agenticore.cli._api_get")
    def test_jobs_api_error(self, mock_get, capsys):
        mock_get.return_value = {"success": False, "error": "internal error"}
        args = SimpleNamespace(limit=20, status=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_jobs(args)
        assert exc_info.value.code == 1
        assert "internal error" in capsys.readouterr().err

    @patch("agenticore.cli._api_get", side_effect=ConnectionError("down"))
    def test_jobs_connection_error(self, mock_get, capsys):
        args = SimpleNamespace(limit=20, status=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_jobs(args)
        assert exc_info.value.code == 1

    @patch("agenticore.cli._api_get")
    def test_jobs_truncates_long_task(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "jobs": [
                {"id": "j1", "status": "queued", "profile": "code", "task": "A" * 100},
            ],
        }
        args = SimpleNamespace(limit=20, status=None)
        _cmd_jobs(args)
        out = capsys.readouterr().out
        # Task should be truncated to 40 chars
        assert "A" * 40 in out
        assert "A" * 41 not in out


# ── job command ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdJob:
    @patch("agenticore.cli._api_get")
    def test_job_details_human(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "job": {
                "id": "j1",
                "status": "succeeded",
                "profile": "code",
                "task": "fix bug",
                "repo_url": "https://github.com/org/repo",
                "exit_code": 0,
                "pr_url": "https://github.com/org/repo/pull/1",
                "created_at": "2024-01-01T00:00:00Z",
                "ended_at": "2024-01-01T00:01:00Z",
            },
        }
        args = SimpleNamespace(job_id="j1", json=False)
        _cmd_job(args)

        out = capsys.readouterr().out
        assert "j1" in out
        assert "succeeded" in out
        assert "code" in out
        assert "fix bug" in out
        assert "Repo:" in out
        assert "Exit:" in out
        assert "PR:" in out
        assert "Created:" in out
        assert "Ended:" in out

    @patch("agenticore.cli._api_get")
    def test_job_details_json_format(self, mock_get, capsys):
        job_data = {"id": "j1", "status": "queued", "task": "test"}
        mock_get.return_value = {"success": True, "job": job_data}
        args = SimpleNamespace(job_id="j1", json=True)
        _cmd_job(args)

        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["id"] == "j1"
        assert parsed["status"] == "queued"

    @patch("agenticore.cli._api_get")
    def test_job_with_error(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "job": {
                "id": "j1",
                "status": "failed",
                "profile": "code",
                "task": "deploy",
                "error": "process crashed",
            },
        }
        args = SimpleNamespace(job_id="j1", json=False)
        _cmd_job(args)
        out = capsys.readouterr().out
        assert "Error:" in out
        assert "process crashed" in out

    @patch("agenticore.cli._api_get")
    def test_job_with_output(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "job": {
                "id": "j1",
                "status": "succeeded",
                "profile": "code",
                "task": "test",
                "output": "All tests passed!",
            },
        }
        args = SimpleNamespace(job_id="j1", json=False)
        _cmd_job(args)
        out = capsys.readouterr().out
        assert "Output:" in out
        assert "All tests passed!" in out

    @patch("agenticore.cli._api_get")
    def test_job_output_truncated(self, mock_get, capsys):
        long_output = "x" * 5000
        mock_get.return_value = {
            "success": True,
            "job": {
                "id": "j1",
                "status": "succeeded",
                "profile": "code",
                "task": "test",
                "output": long_output,
            },
        }
        args = SimpleNamespace(job_id="j1", json=False)
        _cmd_job(args)
        out = capsys.readouterr().out
        # Output truncated to 2000 chars
        assert len(out) < 5000

    @patch("agenticore.cli._api_get")
    def test_job_minimal_fields(self, mock_get, capsys):
        """Job with only required fields doesn't crash."""
        mock_get.return_value = {
            "success": True,
            "job": {"id": "j1", "status": "queued", "profile": "", "task": ""},
        }
        args = SimpleNamespace(job_id="j1", json=False)
        _cmd_job(args)
        out = capsys.readouterr().out
        assert "j1" in out
        # Optional fields not shown
        assert "Repo:" not in out
        assert "Exit:" not in out
        assert "PR:" not in out

    @patch("agenticore.cli._api_get")
    def test_job_not_found(self, mock_get, capsys):
        mock_get.return_value = {"success": False, "error": "Job not found: bad-id"}
        args = SimpleNamespace(job_id="bad-id", json=False)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_job(args)
        assert exc_info.value.code == 1
        assert "not found" in capsys.readouterr().err.lower()


# ── cancel command ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdCancel:
    @patch("agenticore.cli._api_delete")
    def test_cancel_success(self, mock_delete, capsys):
        mock_delete.return_value = {
            "success": True,
            "job": {"id": "j1", "status": "cancelled"},
        }
        args = SimpleNamespace(job_id="j1")
        _cmd_cancel(args)

        mock_delete.assert_called_once_with("/jobs/j1")
        out = capsys.readouterr().out
        assert "j1" in out
        assert "cancelled" in out

    @patch("agenticore.cli._api_delete")
    def test_cancel_api_error(self, mock_delete, capsys):
        mock_delete.return_value = {"success": False, "error": "not found"}
        args = SimpleNamespace(job_id="bad")
        with pytest.raises(SystemExit) as exc_info:
            _cmd_cancel(args)
        assert exc_info.value.code == 1

    @patch("agenticore.cli._api_delete", side_effect=ConnectionError("timeout"))
    def test_cancel_connection_error(self, mock_delete, capsys):
        args = SimpleNamespace(job_id="j1")
        with pytest.raises(SystemExit) as exc_info:
            _cmd_cancel(args)
        assert exc_info.value.code == 1


# ── profiles command ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdProfiles:
    @patch("agenticore.cli._api_get")
    def test_profiles_list(self, mock_get, capsys):
        mock_get.return_value = {
            "success": True,
            "profiles": [
                {
                    "name": "code",
                    "description": "Autonomous coding",
                    "model": "sonnet",
                    "max_turns": 80,
                    "auto_pr": True,
                },
                {"name": "review", "description": "Code reviewer", "model": "haiku", "max_turns": 20, "auto_pr": False},
            ],
        }
        args = SimpleNamespace()
        _cmd_profiles(args)

        out = capsys.readouterr().out
        assert "code" in out
        assert "review" in out
        assert "sonnet" in out
        assert "haiku" in out

    @patch("agenticore.cli._api_get")
    def test_profiles_empty(self, mock_get, capsys):
        mock_get.return_value = {"success": True, "profiles": []}
        args = SimpleNamespace()
        _cmd_profiles(args)
        assert "No profiles found" in capsys.readouterr().out

    @patch("agenticore.cli._api_get")
    def test_profiles_api_error(self, mock_get, capsys):
        mock_get.return_value = {"success": False, "error": "server error"}
        args = SimpleNamespace()
        with pytest.raises(SystemExit) as exc_info:
            _cmd_profiles(args)
        assert exc_info.value.code == 1

    @patch("agenticore.cli._api_get", side_effect=ConnectionError("down"))
    def test_profiles_connection_error(self, mock_get, capsys):
        args = SimpleNamespace()
        with pytest.raises(SystemExit) as exc_info:
            _cmd_profiles(args)
        assert exc_info.value.code == 1


# ── status command ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdStatus:
    @patch("agenticore.cli._api_get")
    def test_status_healthy(self, mock_get, capsys):
        mock_get.return_value = {"status": "ok", "service": "agenticore"}
        args = SimpleNamespace()
        _cmd_status(args)

        out = capsys.readouterr().out
        assert "ok" in out
        assert "agenticore" in out

    @patch("agenticore.cli._api_get", side_effect=ConnectionError("refused"))
    def test_status_unreachable(self, mock_get, capsys):
        args = SimpleNamespace()
        with pytest.raises(SystemExit) as exc_info:
            _cmd_status(args)
        assert exc_info.value.code == 1
        assert "not reachable" in capsys.readouterr().err.lower()

    @patch("agenticore.cli._api_get")
    def test_status_missing_fields(self, mock_get, capsys):
        """Handles missing status/service fields gracefully."""
        mock_get.return_value = {}
        args = SimpleNamespace()
        _cmd_status(args)
        out = capsys.readouterr().out
        assert "unknown" in out


# ── version command ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdVersion:
    def test_version_output(self, capsys):
        args = SimpleNamespace()
        _cmd_version(args)
        out = capsys.readouterr().out
        assert f"agenticore {__version__}" in out


# ── update command ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdUpdate:
    @patch("subprocess.run")
    def test_update_success_same_version(self, mock_run, capsys):
        """Update succeeds but version unchanged = already up to date."""
        mock_run.return_value = MagicMock(returncode=0)
        with patch("agenticore.cli._get_installed_version", return_value=__version__):
            args = SimpleNamespace(source=None)
            _cmd_update(args)
        out = capsys.readouterr().out
        assert "Already up to date" in out

    @patch("subprocess.run")
    def test_update_success_new_version(self, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("agenticore.cli._get_installed_version", return_value="99.0.0"):
            args = SimpleNamespace(source=None)
            _cmd_update(args)
        out = capsys.readouterr().out
        assert "99.0.0" in out

    @patch("subprocess.run")
    def test_update_failure(self, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=1, stderr="pip error")
        args = SimpleNamespace(source=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_update(args)
        assert exc_info.value.code == 1
        assert "failed" in capsys.readouterr().err.lower()

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 120))
    def test_update_timeout(self, mock_run, capsys):
        args = SimpleNamespace(source=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_update(args)
        assert exc_info.value.code == 1
        assert "timed out" in capsys.readouterr().err.lower()

    @patch("subprocess.run")
    def test_update_custom_source(self, mock_run, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("agenticore.cli._get_installed_version", return_value=__version__):
            args = SimpleNamespace(source="git+https://github.com/org/agenticore.git")
            _cmd_update(args)
        cmd = mock_run.call_args[0][0]
        assert "git+https://github.com/org/agenticore.git" in cmd

    @patch("subprocess.run", side_effect=OSError("not found"))
    def test_update_pip_missing(self, mock_run, capsys):
        args = SimpleNamespace(source=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_update(args)
        assert exc_info.value.code == 1


# ── main() entrypoint ────────────────────────────────────────────────────


@pytest.mark.unit
class TestMainEntrypoint:
    def test_no_command_shows_help(self, capsys):
        with patch("sys.argv", ["agenticore"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_version_flag(self, capsys):
        with patch("sys.argv", ["agenticore", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    @patch("agenticore.cli._api_post")
    def test_run_command_dispatches(self, mock_post, capsys):
        mock_post.return_value = {
            "success": True,
            "job": {"id": "j1", "status": "queued", "profile": "code"},
        }
        with patch("sys.argv", ["agenticore", "run", "fix bug"]):
            main()
        mock_post.assert_called_once()
        assert "j1" in capsys.readouterr().out

    @patch("agenticore.server.main")
    def test_serve_command_dispatches(self, mock_server_main):
        with patch("sys.argv", ["agenticore", "serve"]):
            main()
        mock_server_main.assert_called_once()

    def test_version_command_dispatches(self, capsys):
        with patch("sys.argv", ["agenticore", "version"]):
            main()
        assert __version__ in capsys.readouterr().out


# ── _get_installed_version ────────────────────────────────────────────────


@pytest.mark.unit
class TestGetInstalledVersion:
    def test_returns_version_string(self):
        v = _get_installed_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_returns_empty_on_import_error(self):
        with patch("importlib.reload", side_effect=ImportError("boom")):
            v = _get_installed_version()
            assert v == ""


# ── init-shared-fs command ────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdInitSharedFs:
    def test_creates_layout_dirs(self, tmp_path, capsys):
        """init-shared-fs creates profiles, repos, jobs, job-state dirs."""
        from types import SimpleNamespace

        args = SimpleNamespace(shared_root=str(tmp_path / "shared"))
        with patch("agenticore.profiles._defaults_dir", return_value=tmp_path / "no-profiles"):
            _cmd_init_shared_fs(args)

        root = tmp_path / "shared"
        assert (root / "profiles").is_dir()
        assert (root / "repos").is_dir()
        assert (root / "jobs").is_dir()
        assert (root / "job-state").is_dir()
        assert "initialised" in capsys.readouterr().out.lower()

    def test_uses_env_var_when_no_arg(self, tmp_path, monkeypatch, capsys):
        """Uses AGENTICORE_SHARED_FS_ROOT when --shared-root is not set."""
        from types import SimpleNamespace

        root = tmp_path / "from-env"
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(root))
        args = SimpleNamespace(shared_root=None)
        with patch("agenticore.profiles._defaults_dir", return_value=tmp_path / "no-profiles"):
            _cmd_init_shared_fs(args)

        assert (root / "profiles").is_dir()

    def test_exits_when_no_root_configured(self, monkeypatch, capsys):
        """Exits with error when neither arg nor env var is provided."""
        from types import SimpleNamespace

        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)
        args = SimpleNamespace(shared_root=None)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_init_shared_fs(args)
        assert exc_info.value.code == 1
        assert "required" in capsys.readouterr().err.lower()

    def test_copies_bundled_profiles(self, tmp_path, capsys):
        """Copies default profiles to shared FS when defaults dir exists."""
        from types import SimpleNamespace

        # Create a fake defaults dir with one profile
        defaults = tmp_path / "defaults"
        fake_profile = defaults / "code"
        fake_profile.mkdir(parents=True)
        (fake_profile / "profile.yml").write_text("name: code")

        root = tmp_path / "shared"
        args = SimpleNamespace(shared_root=str(root))

        with patch("agenticore.profiles._defaults_dir", return_value=defaults):
            _cmd_init_shared_fs(args)

        assert (root / "profiles" / "code" / "profile.yml").exists()
        assert "code" in capsys.readouterr().out


# ── drain command ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCmdDrain:
    def test_drain_no_running_jobs(self, capsys):
        """drain exits cleanly when no running jobs on this pod."""
        from types import SimpleNamespace
        from unittest.mock import patch

        args = SimpleNamespace(timeout=10)
        with (
            patch.dict("os.environ", {"AGENTICORE_POD_NAME": "test-pod-0", "REDIS_URL": ""}),
            patch("agenticore.jobs.list_jobs", return_value=[]),
        ):
            _cmd_drain(args)

        out = capsys.readouterr().out
        assert "complete" in out.lower()

    def test_drain_redis_unavailable_continues(self, capsys):
        """drain works even when Redis is not available."""
        from types import SimpleNamespace

        args = SimpleNamespace(timeout=10)
        with (
            patch.dict("os.environ", {"AGENTICORE_POD_NAME": "pod-0", "REDIS_URL": "redis://bad:6379"}),
            patch("redis.Redis.from_url", side_effect=Exception("connection refused")),
            patch("agenticore.jobs.list_jobs", return_value=[]),
        ):
            _cmd_drain(args)

        err = capsys.readouterr().err
        assert "unavailable" in err.lower()

    def test_drain_timeout_reached(self, capsys):
        """drain prints timeout message when jobs don't finish."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        args = SimpleNamespace(timeout=1)

        # Simulate a job that never finishes (always running on our pod)
        running_job = MagicMock()
        running_job.pod_name = "pod-0"

        with (
            patch.dict("os.environ", {"AGENTICORE_POD_NAME": "pod-0", "REDIS_URL": ""}),
            patch("agenticore.jobs.list_jobs", return_value=[running_job]),
            patch("time.sleep"),
        ):
            _cmd_drain(args)

        err = capsys.readouterr().err
        assert "timeout" in err.lower()

    def test_drain_uses_hostname_when_no_pod_name(self, capsys):
        """drain falls back to hostname when AGENTICORE_POD_NAME is unset."""
        from types import SimpleNamespace

        args = SimpleNamespace(timeout=5)
        with (
            patch.dict("os.environ", {"AGENTICORE_POD_NAME": "", "REDIS_URL": ""}),
            patch("agenticore.jobs.list_jobs", return_value=[]),
            patch("os.uname") as mock_uname,
        ):
            mock_uname.return_value = MagicMock(nodename="my-host")
            _cmd_drain(args)

        out = capsys.readouterr().out
        assert "my-host" in out


# ── main() dispatches new commands ───────────────────────────────────────


@pytest.mark.unit
class TestMainDispatchesNewCommands:
    def test_init_shared_fs_dispatches(self, tmp_path, capsys):
        root = tmp_path / "shared"
        with (
            patch("sys.argv", ["agenticore", "init-shared-fs", "--shared-root", str(root)]),
            patch("agenticore.profiles._defaults_dir", return_value=tmp_path / "no-defaults"),
        ):
            main()
        assert (root / "profiles").is_dir()

    def test_drain_dispatches(self, capsys):
        with (
            patch("sys.argv", ["agenticore", "drain", "--timeout", "5"]),
            patch.dict("os.environ", {"AGENTICORE_POD_NAME": "pod-0", "REDIS_URL": ""}),
            patch("agenticore.jobs.list_jobs", return_value=[]),
        ):
            main()
        assert "complete" in capsys.readouterr().out.lower()
