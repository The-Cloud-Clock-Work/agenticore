"""Unit tests for repos module."""

import os
from unittest.mock import MagicMock, patch

import pytest

from agenticore.config import reset_config
from agenticore.repos import _repo_key, resolve_github_token


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestRepoKey:
    def test_deterministic(self):
        """Same URL always produces same key."""
        url = "https://github.com/org/repo.git"
        assert _repo_key(url) == _repo_key(url)

    def test_different_urls_different_keys(self):
        k1 = _repo_key("https://github.com/org/repo1.git")
        k2 = _repo_key("https://github.com/org/repo2.git")
        assert k1 != k2

    def test_length_12(self):
        key = _repo_key("https://github.com/org/repo.git")
        assert len(key) == 12

    def test_hex_chars_only(self):
        key = _repo_key("https://example.com/repo")
        assert all(c in "0123456789abcdef" for c in key)


@pytest.mark.unit
class TestResolveGithubToken:
    @patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False)
    def test_github_app_token_used(self):
        """GitHub App configured + returns token → uses it."""
        mock_auth = MagicMock()
        mock_auth.enabled = True
        mock_auth.get_token.return_value = "ghs_app_token"

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() == "ghs_app_token"

    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_static"}, clear=False)
    def test_no_app_uses_static(self):
        """No App → static GITHUB_TOKEN used."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() == "ghp_static"

    @patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False)
    def test_nothing_configured_returns_none(self):
        """No auth configured → returns None."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() is None


@pytest.mark.unit
class TestAuthenticatedUrlRemoved:
    def test_no_authenticated_url_export(self):
        """_authenticated_url no longer exists in repos module."""
        import agenticore.repos as repos_mod

        assert not hasattr(repos_mod, "_authenticated_url")
