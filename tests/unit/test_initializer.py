"""Unit tests for agent_mode/initializer.py."""

import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from agenticore.agent_mode.agent import _load_system_prompt, reset_system_prompt_cache
from agenticore.agent_mode.initializer import (
    _clone_package_repo,
    _run_startup_scripts,
    _validate_package_dir,
)
from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    reset_system_prompt_cache()
    yield
    reset_config()
    reset_system_prompt_cache()


@pytest.mark.unit
class TestValidatePackageDir:
    def test_valid_dir(self, tmp_path):
        pkg = tmp_path / "package"
        pkg.mkdir()
        (pkg / "CLAUDE.md").write_text("# Test")
        _validate_package_dir(str(pkg))  # should not raise

    def test_valid_dir_without_claude_md(self, tmp_path):
        """Package dir is valid even without CLAUDE.md (it's optional)."""
        pkg = tmp_path / "package"
        pkg.mkdir()
        _validate_package_dir(str(pkg))  # should not raise

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="does not exist"):
            _validate_package_dir(str(tmp_path / "nonexistent"))


@pytest.mark.unit
class TestRunStartupScripts:
    def test_no_runners_dir(self, tmp_path):
        pkg = tmp_path / "package"
        pkg.mkdir()
        _run_startup_scripts(str(pkg))  # should not raise

    def test_runs_sh_script(self, tmp_path):
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        script = runners / "001_setup.sh"
        script.write_text("#!/bin/bash\necho 'hello from setup'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        _run_startup_scripts(str(pkg))  # should complete without error

    def test_runs_py_script(self, tmp_path):
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        script = runners / "001_setup.py"
        script.write_text("print('hello from python')\n")

        _run_startup_scripts(str(pkg))  # should complete without error

    def test_skips_dotfiles(self, tmp_path):
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        (runners / ".hidden").write_text("skip me")
        (runners / "001_run.sh").write_text("#!/bin/bash\necho ok\n")

        _run_startup_scripts(str(pkg))  # should only run 001_run.sh

    def test_skips_unknown_extension(self, tmp_path):
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        (runners / "001_data.json").write_text("{}")

        _run_startup_scripts(str(pkg))  # should skip json files

    def test_scripts_run_in_sorted_order(self, tmp_path):
        """Scripts run sorted by filename (prefix order)."""
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        marker = tmp_path / "order.txt"
        (runners / "002_second.sh").write_text(f"#!/bin/bash\necho 2 >> {marker}\n")
        (runners / "001_first.sh").write_text(f"#!/bin/bash\necho 1 >> {marker}\n")
        (runners / "003_third.sh").write_text(f"#!/bin/bash\necho 3 >> {marker}\n")

        for s in runners.iterdir():
            s.chmod(s.stat().st_mode | stat.S_IEXEC)

        _run_startup_scripts(str(pkg))

        lines = marker.read_text().strip().splitlines()
        assert lines == ["1", "2", "3"]

    def test_failed_script_does_not_crash(self, tmp_path):
        """A failing script should not prevent subsequent scripts from running."""
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        marker = tmp_path / "ran.txt"
        (runners / "001_fail.sh").write_text("#!/bin/bash\nexit 1\n")
        (runners / "002_ok.sh").write_text(f"#!/bin/bash\necho ok >> {marker}\n")

        for s in runners.iterdir():
            s.chmod(s.stat().st_mode | stat.S_IEXEC)

        _run_startup_scripts(str(pkg))  # should not raise

        assert marker.exists()
        assert "ok" in marker.read_text()

    def test_empty_runners_dir(self, tmp_path):
        """Empty runners/ directory is handled gracefully."""
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        _run_startup_scripts(str(pkg))  # should not raise

    def test_sh_script_without_exec_bit(self, tmp_path):
        """Shell scripts without +x bit still run (we chmod them)."""
        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        script = runners / "001_noexec.sh"
        script.write_text("#!/bin/bash\necho 'hello'\n")
        # Intentionally NOT setting executable bit

        _run_startup_scripts(str(pkg))  # should handle it


@pytest.mark.unit
class TestClonePackageRepo:
    def test_clone_success(self, tmp_path):
        """Mocked git clone creates the expected directory structure."""
        target = tmp_path / "app"

        def mock_run(cmd, **kwargs):
            clone_dir = target / "_clone_tmp"
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir()
            pkg = clone_dir / "package"
            pkg.mkdir()
            (pkg / "CLAUDE.md").write_text("# Agent")
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("agenticore.repos.resolve_github_token", return_value="ghp_test"),
            patch("agenticore.git_credentials.git_askpass_env"),
        ):
            _clone_package_repo("https://github.com/org/agent.git", "main", str(target))

        assert (target / "package" / "CLAUDE.md").exists()

    def test_clone_failure_raises(self, tmp_path):
        """Git clone failure raises RuntimeError."""
        target = tmp_path / "app"

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "fatal: repo not found"
            return result

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("agenticore.repos.resolve_github_token", return_value=None),
            patch("agenticore.git_credentials.git_askpass_env"),
        ):
            with pytest.raises(RuntimeError, match="Git clone failed"):
                _clone_package_repo("https://github.com/org/bad.git", "main", str(target))


@pytest.mark.unit
class TestInitializeAgentMode:
    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "",
            "PACKAGE_REPO_URL": "",
            "REDIS_URL": "",
        },
    )
    def test_init_with_local_package(self, tmp_path):
        from agenticore.agent_mode.initializer import initialize_agent_mode

        pkg = tmp_path / "package"
        pkg.mkdir()
        (pkg / "CLAUDE.md").write_text("# Test Agent")

        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg)}):
            reset_config()
            initialize_agent_mode()

    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "/nonexistent/pkg",
            "PACKAGE_REPO_URL": "",
            "REDIS_URL": "",
        },
    )
    def test_init_fails_on_missing_package(self):
        from agenticore.agent_mode.initializer import initialize_agent_mode

        reset_config()
        with pytest.raises(SystemExit):
            initialize_agent_mode()

    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "",
            "PACKAGE_REPO_URL": "",
            "REDIS_URL": "",
        },
    )
    def test_init_caches_system_prompt(self, tmp_path):
        from agenticore.agent_mode.initializer import initialize_agent_mode

        pkg = tmp_path / "package"
        pkg.mkdir()
        (pkg / "system.md").write_text("You are a test agent.")

        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg)}):
            reset_config()
            initialize_agent_mode()

        # After init, system prompt should be cached
        cached = _load_system_prompt(str(pkg))
        assert cached == "You are a test agent."

    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "",
            "PACKAGE_REPO_URL": "",
            "REDIS_URL": "",
        },
    )
    def test_init_runs_startup_scripts(self, tmp_path):
        from agenticore.agent_mode.initializer import initialize_agent_mode

        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        marker = tmp_path / "init_marker.txt"
        script = runners / "001_init.sh"
        script.write_text(f"#!/bin/bash\necho INIT >> {marker}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg)}):
            reset_config()
            bg_threads = initialize_agent_mode()
            for t in bg_threads:
                t.join(timeout=10)

        assert marker.exists()
        assert "INIT" in marker.read_text()

    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "",
            "PACKAGE_REPO_URL": "",
            "REDIS_URL": "",
        },
    )
    def test_py_script_nonzero_exit_continues(self, tmp_path):
        """A failing Python script should not prevent subsequent scripts from running."""
        from agenticore.agent_mode.initializer import initialize_agent_mode

        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        marker = tmp_path / "py_marker.txt"
        (runners / "001_fail.py").write_text("import sys; sys.exit(1)\n")
        (runners / "002_ok.py").write_text(f"with open('{marker}', 'w') as f: f.write('ok')\n")

        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg)}):
            reset_config()
            bg_threads = initialize_agent_mode()
            for t in bg_threads:
                t.join(timeout=10)

        assert marker.exists()
        assert "ok" in marker.read_text()

    def test_script_timeout_raises(self, tmp_path):
        """Script exceeding timeout raises TimeoutExpired."""
        import subprocess

        pkg = tmp_path / "package"
        runners = pkg / "runners"
        runners.mkdir(parents=True)

        script = runners / "001_slow.sh"
        script.write_text("#!/bin/bash\nsleep 300\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            with pytest.raises(subprocess.TimeoutExpired):
                _run_startup_scripts(str(pkg))

    def test_clone_timeout_raises(self, tmp_path):
        """Git clone exceeding timeout raises TimeoutExpired."""
        import subprocess

        target = tmp_path / "app"

        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git clone", 120)),
            patch("agenticore.repos.resolve_github_token", return_value=None),
            patch("agenticore.git_credentials.git_askpass_env"),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                _clone_package_repo("https://github.com/org/repo.git", "main", str(target))

    @patch.dict(
        os.environ,
        {
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "",
            "PACKAGE_REPO_URL": "https://github.com/org/agent.git",
            "REDIS_URL": "",
        },
    )
    def test_init_with_repo_clone_failure_exits(self, tmp_path):
        """Failed repo clone causes sys.exit."""
        from agenticore.agent_mode.initializer import initialize_agent_mode

        with (
            patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(tmp_path / "package")}),
            patch("agenticore.agent_mode.initializer._clone_package_repo", side_effect=RuntimeError("clone failed")),
        ):
            reset_config()
            with pytest.raises(SystemExit):
                initialize_agent_mode()
