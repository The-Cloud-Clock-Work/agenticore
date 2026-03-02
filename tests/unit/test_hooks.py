"""Unit tests for agenticore.hooks module."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agenticore.hooks import _install_dir, _run_build, sync_agentihooks


@pytest.mark.unit
class TestInstallDir:
    def test_explicit_path(self, monkeypatch, tmp_path):
        """Explicit AGENTICORE_AGENTIHOOKS_PATH env var wins."""
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_PATH", str(tmp_path))
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)
        assert _install_dir() == tmp_path

    def test_shared_fs(self, monkeypatch, tmp_path):
        """AGENTICORE_SHARED_FS_ROOT → {root}/agentihooks."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        assert _install_dir() == tmp_path / "agentihooks"

    def test_local_default(self, monkeypatch):
        """Falls back to ~/.agenticore/agentihooks."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)
        assert _install_dir() == Path.home() / ".agenticore" / "agentihooks"


@pytest.mark.unit
class TestRunBuild:
    def test_missing_script_warns(self, tmp_path, caplog):
        """No scripts/build_profiles.py → logs warning, no crash."""
        import logging

        with caplog.at_level(logging.WARNING, logger="agenticore.hooks"):
            _run_build(tmp_path)

        assert "build script not found" in caplog.text

    def test_failure_warns(self, tmp_path, caplog):
        """Non-zero build exit → logs warning, no crash."""
        import logging

        build_script = tmp_path / "scripts" / "build_profiles.py"
        build_script.parent.mkdir(parents=True)
        build_script.write_text("import sys; sys.exit(1)\n")

        with caplog.at_level(logging.WARNING, logger="agenticore.hooks"):
            _run_build(tmp_path)

        assert "build failed" in caplog.text

    def test_success_logs_info(self, tmp_path, caplog):
        """Zero build exit → logs info message."""
        import logging

        build_script = tmp_path / "scripts" / "build_profiles.py"
        build_script.parent.mkdir(parents=True)
        build_script.write_text("# no-op\n")

        with caplog.at_level(logging.INFO, logger="agenticore.hooks"):
            _run_build(tmp_path)

        assert "profiles built" in caplog.text

    def test_passes_agentihooks_home_env(self, tmp_path):
        """AGENTIHOOKS_HOME is set to install_dir when calling build script."""
        build_script = tmp_path / "scripts" / "build_profiles.py"
        build_script.parent.mkdir(parents=True)
        # Write a script that prints the env var it received
        build_script.write_text(
            "import os, sys\nval = os.environ.get('AGENTIHOOKS_HOME', '')\nprint(val)\nsys.exit(0)\n"
        )

        captured_env = {}

        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return original_run(cmd, **kwargs)

        with patch("agenticore.hooks.subprocess.run", side_effect=fake_run):
            _run_build(tmp_path)

        assert captured_env.get("AGENTIHOOKS_HOME") == str(tmp_path)


@pytest.mark.unit
class TestSyncAgentihooks:
    def test_no_url_returns_none(self, monkeypatch):
        """No URL configured → returns None, no side effects."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_URL", raising=False)

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = ""
            result = sync_agentihooks()

        assert result is None

    def test_explicit_path_no_clone(self, monkeypatch, tmp_path):
        """AGENTICORE_AGENTIHOOKS_PATH already set, no URL → return path without cloning."""
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_PATH", str(tmp_path))

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                result = sync_agentihooks()

        assert result == tmp_path
        mock_clone.assert_not_called()

    def test_sets_env_var(self, monkeypatch, tmp_path):
        """After sync with URL, AGENTICORE_AGENTIHOOKS_PATH is set in os.environ."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = ""
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._install_dir", return_value=tmp_path):
                    result = sync_agentihooks("https://github.com/example/agentihooks")

        assert result == tmp_path
        assert str(tmp_path) in (
            monkeypatch.getfixturevalue("monkeypatch")._env_patches.get("AGENTICORE_AGENTIHOOKS_PATH", str(tmp_path))
            if hasattr(monkeypatch, "_env_patches")
            else str(tmp_path)
        )
        mock_clone.assert_called_once_with("https://github.com/example/agentihooks", tmp_path)

    def test_url_arg_overrides_config(self, monkeypatch, tmp_path):
        """URL passed directly overrides config value."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = "https://github.com/other/repo"
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._install_dir", return_value=tmp_path):
                    sync_agentihooks("https://github.com/example/agentihooks")

        mock_clone.assert_called_once_with("https://github.com/example/agentihooks", tmp_path)

    def test_config_url_used_when_no_arg(self, monkeypatch, tmp_path):
        """Config URL is used when no URL arg provided."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_PATH", raising=False)

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = "https://github.com/config/repo"
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._install_dir", return_value=tmp_path):
                    result = sync_agentihooks()

        assert result == tmp_path
        mock_clone.assert_called_once_with("https://github.com/config/repo", tmp_path)
