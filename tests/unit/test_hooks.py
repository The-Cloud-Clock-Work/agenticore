"""Unit tests for agenticore.hooks module."""

from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from agenticore.config import RuntimePaths, reset_config
from agenticore.hooks import (
    _dir_from_url,
    _install_dir,
    sync_agentihooks,
    sync_agentihub,
    sync_bundle,
)


@pytest.fixture(autouse=True)
def _reset_config_singleton(monkeypatch):
    """Reset the cached config singleton before every test in this module so
    env-var changes (AGENTICORE_AGENTIHOOKS_URL, AGENTICORE_SHARED_FS_ROOT)
    take effect on the next get_config() call.
    """
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestNoAuthenticatedUrlImport:
    def test_hooks_does_not_import_authenticated_url(self):
        """_authenticated_url is no longer imported by hooks module."""
        import agenticore.hooks as hooks_mod

        assert not hasattr(hooks_mod, "_authenticated_url")


@pytest.mark.unit
class TestDirFromUrl:
    def test_strips_dot_git(self):
        assert _dir_from_url("https://github.com/foo/bar.git") == "bar"

    def test_no_dot_git(self):
        assert _dir_from_url("https://github.com/foo/bar") == "bar"

    def test_trailing_slash(self):
        assert _dir_from_url("https://github.com/foo/bar/") == "bar"

    def test_real_repo_names(self):
        assert _dir_from_url("https://github.com/x/agentihooks-bundle.git") == "agentihooks-bundle"
        assert _dir_from_url("https://github.com/x/agentihub") == "agentihub"
        assert _dir_from_url("https://github.com/x/agentihooks.git") == "agentihooks"


@pytest.mark.unit
class TestInstallDir:
    def test_url_under_shared_fs(self, monkeypatch, tmp_path):
        """URL set + SHARED_FS_ROOT set → {root}/<dir-from-url>."""
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_URL", "https://github.com/x/agentihooks")
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        assert _install_dir() == tmp_path / "agentihooks"

    def test_url_with_dot_git(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_URL", "https://github.com/x/agentihooks.git")
        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        assert _install_dir() == tmp_path / "agentihooks"

    def test_no_url_returns_none(self, monkeypatch):
        """No URL → None (repo isn't part of this deployment)."""
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_URL", raising=False)
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)
        assert _install_dir() is None


@pytest.mark.unit
class TestSyncAgentihooks:
    """agentihooks is a PyPI dependency. URL set → clone + editable install
    + populate cfg.runtime.agentihooks_dir; URL unset → PyPI."""

    def test_no_url_falls_through_to_pypi(self, monkeypatch):
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_URL", raising=False)

        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = ""
            mock_cfg.return_value.agentihooks_branch = ""
            mock_cfg.return_value.runtime = RuntimePaths()
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._pip_install_editable") as mock_pip_e:
                    with patch("agenticore.hooks._pip_install_pypi") as mock_pypi:
                        result = sync_agentihooks()

        assert result is None
        mock_clone.assert_not_called()
        mock_pip_e.assert_not_called()
        mock_pypi.assert_called_once_with("agentihooks")

    def test_url_clones_then_pip_installs(self, monkeypatch, tmp_path):
        """URL set → clone + uv pip install -e <dest> + cfg.runtime updated."""
        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)

        dest = tmp_path / "agentihooks-clone"  # does not exist → clone path
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = ""
            mock_cfg.return_value.agentihooks_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._pip_install_editable") as mock_pip:
                    with patch("agenticore.hooks._install_dir", return_value=dest):
                        result = sync_agentihooks("https://github.com/example/agentihooks")

        assert result == dest
        mock_clone.assert_called_once_with("https://github.com/example/agentihooks", dest, ANY)
        mock_pip.assert_called_once_with(dest)
        assert runtime.agentihooks_dir == dest

    def test_url_arg_overrides_config(self, tmp_path):
        dest = tmp_path / "agentihooks-clone"
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = "https://github.com/other/repo"
            mock_cfg.return_value.agentihooks_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._pip_install_editable"):
                    with patch("agenticore.hooks._install_dir", return_value=dest):
                        sync_agentihooks("https://github.com/example/agentihooks")

        mock_clone.assert_called_once_with("https://github.com/example/agentihooks", dest, ANY)

    def test_config_url_used_when_no_arg(self, tmp_path):
        dest = tmp_path / "agentihooks-clone"
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_url = "https://github.com/config/repo"
            mock_cfg.return_value.agentihooks_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch") as mock_clone:
                with patch("agenticore.hooks._pip_install_editable"):
                    with patch("agenticore.hooks._install_dir", return_value=dest):
                        result = sync_agentihooks()

        assert result == dest
        mock_clone.assert_called_once_with("https://github.com/config/repo", dest, ANY)


@pytest.mark.unit
class TestSyncBundle:
    def test_no_url_returns_none(self):
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_bundle_url = ""
            mock_cfg.return_value.runtime = RuntimePaths()
            assert sync_bundle() is None

    def test_url_clones_and_populates_runtime(self, tmp_path):
        dest = tmp_path / "bundle-clone"
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihooks_bundle_url = "https://github.com/x/agentihooks-bundle"
            mock_cfg.return_value.agentihooks_bundle_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch_bundle") as mock_clone:
                with patch("agenticore.hooks._bundle_dir", return_value=dest):
                    result = sync_bundle()

        assert result == dest
        mock_clone.assert_called_once()
        assert runtime.bundle_dir == dest


@pytest.mark.unit
class TestSyncAgentihub:
    def test_no_url_returns_none(self):
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihub_url = ""
            mock_cfg.return_value.runtime = RuntimePaths()
            assert sync_agentihub() is None

    def test_url_clones_and_populates_runtime(self, tmp_path):
        dest = tmp_path / "hub-clone"
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihub_url = "https://github.com/x/agentihub"
            mock_cfg.return_value.agentihub_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch_agentihub") as mock_clone:
                with patch("agenticore.hooks._agentihub_install_dir", return_value=dest):
                    result = sync_agentihub()

        assert result == dest
        mock_clone.assert_called_once()
        assert runtime.hub_dir == dest

    def test_pre_mounted_dir_skips_clone(self, tmp_path):
        """Pre-existing dir without .git → trust as bind-mount, no clone."""
        dest = tmp_path / "hub-mount"
        dest.mkdir()  # exists but no .git
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihub_url = "https://github.com/x/agentihub"
            mock_cfg.return_value.agentihub_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch_agentihub") as mock_clone:
                with patch("agenticore.hooks._agentihub_install_dir", return_value=dest):
                    result = sync_agentihub()

        assert result == dest
        mock_clone.assert_not_called()
        assert runtime.hub_dir == dest

    def test_existing_clone_does_fetch(self, tmp_path):
        """Pre-existing dir with .git → fetch+reset, not first clone."""
        dest = tmp_path / "hub-existing"
        (dest / ".git").mkdir(parents=True)
        runtime = RuntimePaths()
        with patch("agenticore.hooks.get_config") as mock_cfg:
            mock_cfg.return_value.agentihub_url = "https://github.com/x/agentihub"
            mock_cfg.return_value.agentihub_branch = "main"
            mock_cfg.return_value.runtime = runtime
            mock_cfg.return_value.repos.shared_fs_root = ""
            with patch("agenticore.hooks._clone_or_fetch_agentihub") as mock_clone:
                with patch("agenticore.hooks._agentihub_install_dir", return_value=dest):
                    result = sync_agentihub()

        assert result == dest
        mock_clone.assert_called_once()


@pytest.mark.unit
class TestHubBundleUrlCollision:
    """When hub URL == bundle URL, both consumers resolve to the same dir.
    The clone lock must be path-keyed so concurrent boot-time syncs serialize."""

    def test_same_url_yields_same_path(self, monkeypatch, tmp_path):
        from agenticore.hooks import resolve_repo_paths

        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        monkeypatch.setenv("AGENTICORE_AGENTIHUB_URL", "https://github.com/x/agentibrain-kernel")
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_BUNDLE_URL", "https://github.com/x/agentibrain-kernel")

        _, bundle, hub = resolve_repo_paths()
        assert hub == bundle == tmp_path / "agentibrain-kernel"


@pytest.mark.unit
class TestResolveRepoPaths:
    def test_url_derived_dirs_under_shared_fs(self, monkeypatch, tmp_path):
        from agenticore.hooks import resolve_repo_paths

        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_URL", "https://github.com/x/agentihooks.git")
        monkeypatch.setenv("AGENTICORE_AGENTIHOOKS_BUNDLE_URL", "https://github.com/x/agentihooks-bundle")
        monkeypatch.setenv("AGENTICORE_AGENTIHUB_URL", "https://github.com/x/agentihub.git")

        hooks, bundle, hub = resolve_repo_paths()
        assert hooks == tmp_path / "agentihooks"
        assert bundle == tmp_path / "agentihooks-bundle"
        assert hub == tmp_path / "agentihub"

    def test_unset_urls_yield_none(self, monkeypatch, tmp_path):
        from agenticore.hooks import resolve_repo_paths

        monkeypatch.setenv("AGENTICORE_SHARED_FS_ROOT", str(tmp_path))
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_URL", raising=False)
        monkeypatch.delenv("AGENTICORE_AGENTIHOOKS_BUNDLE_URL", raising=False)
        monkeypatch.delenv("AGENTICORE_AGENTIHUB_URL", raising=False)

        hooks, bundle, hub = resolve_repo_paths()
        assert hooks is None and bundle is None and hub is None

    def test_no_shared_fs_root_falls_back_to_local(self, monkeypatch):
        from agenticore.hooks import resolve_repo_paths

        monkeypatch.delenv("AGENTICORE_SHARED_FS_ROOT", raising=False)
        monkeypatch.setenv("AGENTICORE_AGENTIHUB_URL", "https://github.com/x/agentihub")

        _, _, hub = resolve_repo_paths()
        assert hub == Path.home() / ".agenticore" / "agentihub"
