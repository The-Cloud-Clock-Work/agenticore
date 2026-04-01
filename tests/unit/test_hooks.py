"""Unit tests for agenticore.hooks module."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agenticore.hooks import (
    _install_dir,
    start_bundle_watcher,
    start_agentihub_watcher,
    start_sync_watcher,
    sync_agentihooks,
)


@pytest.mark.unit
class TestNoAuthenticatedUrlImport:
    def test_hooks_does_not_import_authenticated_url(self):
        """_authenticated_url is no longer imported by hooks module."""
        import agenticore.hooks as hooks_mod

        assert not hasattr(hooks_mod, "_authenticated_url")


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
                    with patch.dict(os.environ, {}, clear=False):
                        result = sync_agentihooks("https://github.com/example/agentihooks")
                        assert result == tmp_path
                        assert os.environ.get("AGENTICORE_AGENTIHOOKS_PATH") == str(tmp_path)
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



@pytest.mark.unit
class TestSyncWatcher:
    def test_returns_daemon_thread(self, tmp_path):
        """start_sync_watcher returns a started daemon thread."""
        with patch("agenticore.hooks._clone_or_fetch"):
            t = start_sync_watcher("https://example.com/repo", tmp_path, interval=9999)
        assert t.daemon is True
        assert t.is_alive()
        t.join(timeout=0)  # don't block

    def test_calls_clone_or_fetch_after_interval(self, tmp_path):
        """Watcher thread calls _clone_or_fetch after each sleep interval."""
        import agenticore.hooks as hooks_mod

        calls = []

        def fake_clone(url, dest):
            calls.append((url, dest))

        call_count = [0]

        def stop_after_one(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch", side_effect=fake_clone):
            with patch.object(hooks_mod.time, "sleep", side_effect=stop_after_one):
                t = start_sync_watcher("https://example.com/repo", tmp_path, interval=1)
                t.join(timeout=2)

        assert len(calls) >= 1

    def test_handles_clone_exception_without_crashing(self, tmp_path, caplog):
        """Exception in _clone_or_fetch is caught and logged as warning; thread keeps running."""
        import logging

        import agenticore.hooks as hooks_mod

        call_count = [0]

        def fail_then_stop(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch", side_effect=Exception("network error")):
            with patch.object(hooks_mod.time, "sleep", side_effect=fail_then_stop):
                with caplog.at_level(logging.WARNING, logger="agenticore.hooks"):
                    t = start_sync_watcher("https://example.com/repo", tmp_path, interval=1)
                    t.join(timeout=2)

        assert "hot-reload failed" in caplog.text


@pytest.mark.unit
class TestBundleWatcher:
    def test_returns_daemon_thread(self, tmp_path):
        """start_bundle_watcher returns a started daemon thread."""
        with patch("agenticore.hooks._clone_or_fetch_bundle"):
            t = start_bundle_watcher("https://example.com/bundle", tmp_path, interval=9999)
        assert t.daemon is True
        assert t.is_alive()
        t.join(timeout=0)

    def test_calls_clone_or_fetch_bundle_after_interval(self, tmp_path):
        """Watcher thread calls _clone_or_fetch_bundle after each sleep interval."""
        import agenticore.hooks as hooks_mod

        calls = []

        def fake_clone(url, dest):
            calls.append((url, dest))

        call_count = [0]

        def stop_after_one(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch_bundle", side_effect=fake_clone):
            with patch.object(hooks_mod.time, "sleep", side_effect=stop_after_one):
                t = start_bundle_watcher("https://example.com/bundle", tmp_path, interval=1)
                t.join(timeout=2)

        assert len(calls) >= 1

    def test_handles_exception_without_crashing(self, tmp_path, caplog):
        """Exception in _clone_or_fetch_bundle is caught and logged; thread keeps running."""
        import logging

        import agenticore.hooks as hooks_mod

        call_count = [0]

        def fail_then_stop(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch_bundle", side_effect=Exception("network error")):
            with patch.object(hooks_mod.time, "sleep", side_effect=fail_then_stop):
                with caplog.at_level(logging.WARNING, logger="agenticore.hooks"):
                    t = start_bundle_watcher("https://example.com/bundle", tmp_path, interval=1)
                    t.join(timeout=2)

        assert "agentihooks-bundle hot-reload failed" in caplog.text


@pytest.mark.unit
class TestAgentihubWatcher:
    def test_returns_daemon_thread(self, tmp_path):
        """start_agentihub_watcher returns a started daemon thread."""
        with patch("agenticore.hooks._clone_or_fetch_agentihub"):
            t = start_agentihub_watcher("https://example.com/agentihub", tmp_path, interval=9999)
        assert t.daemon is True
        assert t.is_alive()
        t.join(timeout=0)

    def test_calls_clone_or_fetch_agentihub_after_interval(self, tmp_path):
        """Watcher thread calls _clone_or_fetch_agentihub after each sleep interval."""
        import agenticore.hooks as hooks_mod

        calls = []

        def fake_clone(url, dest):
            calls.append((url, dest))

        call_count = [0]

        def stop_after_one(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch_agentihub", side_effect=fake_clone):
            with patch.object(hooks_mod.time, "sleep", side_effect=stop_after_one):
                t = start_agentihub_watcher("https://example.com/agentihub", tmp_path, interval=1)
                t.join(timeout=2)

        assert len(calls) >= 1

    def test_handles_exception_without_crashing(self, tmp_path, caplog):
        """Exception in _clone_or_fetch_agentihub is caught and logged; thread keeps running."""
        import logging

        import agenticore.hooks as hooks_mod

        call_count = [0]

        def fail_then_stop(n):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise SystemExit

        with patch("agenticore.hooks._clone_or_fetch_agentihub", side_effect=Exception("network error")):
            with patch.object(hooks_mod.time, "sleep", side_effect=fail_then_stop):
                with caplog.at_level(logging.WARNING, logger="agenticore.hooks"):
                    t = start_agentihub_watcher("https://example.com/agentihub", tmp_path, interval=1)
                    t.join(timeout=2)

        assert "agentihub hot-reload failed" in caplog.text
