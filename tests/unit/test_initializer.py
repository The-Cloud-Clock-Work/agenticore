"""Unit tests for agent_mode/initializer.py."""

import os
import stat
from unittest.mock import patch

import pytest

from agenticore.agent_mode.agent import reset_system_prompt_cache
from agenticore.agent_mode.initializer import (
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
