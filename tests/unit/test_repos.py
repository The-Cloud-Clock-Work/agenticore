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
    @patch.dict(os.environ, {"GITHUB_TOKEN": "", "AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""}, clear=False)
    def test_github_app_token_used(self):
        """GitHub App configured + returns token → uses it."""
        mock_auth = MagicMock()
        mock_auth.enabled = True
        mock_auth.get_token.return_value = "ghs_app_token"

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() == "ghs_app_token"

    @patch.dict(
        os.environ, {"GITHUB_TOKEN": "", "AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"}, clear=False
    )
    def test_github_app_fails_falls_to_broker(self):
        """GitHub App configured but fails → falls through to broker."""
        mock_auth = MagicMock()
        mock_auth.enabled = True
        mock_auth.get_token.return_value = None

        mock_client = MagicMock()
        mock_client.enabled = True
        mock_client.get_credential.return_value = "gh_broker_tok"

        with (
            patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth),
            patch("agenticore.auth_client.AuthClient", return_value=mock_client),
        ):
            assert resolve_github_token() == "gh_broker_tok"

    @patch.dict(
        os.environ, {"GITHUB_TOKEN": "ghp_static", "AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""}, clear=False
    )
    def test_no_app_no_broker_uses_static(self):
        """No App, no broker → static GITHUB_TOKEN used."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() == "ghp_static"

    @patch.dict(os.environ, {"GITHUB_TOKEN": "", "AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""}, clear=False)
    def test_nothing_configured_returns_none(self):
        """No auth configured → returns None."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        with patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth):
            assert resolve_github_token() is None

    @patch.dict(
        os.environ,
        {"GITHUB_TOKEN": "ghp_fallback", "AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_returns_token(self):
        """Broker returns token → uses it (not static)."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        mock_client = MagicMock()
        mock_client.enabled = True
        mock_client.get_credential.return_value = "gh_broker"

        with (
            patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth),
            patch("agenticore.auth_client.AuthClient", return_value=mock_client),
        ):
            assert resolve_github_token() == "gh_broker"

    @patch.dict(
        os.environ,
        {"GITHUB_TOKEN": "ghp_fallback", "AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_fails_falls_to_static(self):
        """Broker configured but returns None → falls through to static."""
        mock_auth = MagicMock()
        mock_auth.enabled = False

        mock_client = MagicMock()
        mock_client.enabled = True
        mock_client.get_credential.return_value = None

        with (
            patch("agenticore.github_app.get_github_app_auth", return_value=mock_auth),
            patch("agenticore.auth_client.AuthClient", return_value=mock_client),
        ):
            assert resolve_github_token() == "ghp_fallback"


@pytest.mark.unit
class TestAuthenticatedUrlRemoved:
    def test_no_authenticated_url_export(self):
        """_authenticated_url no longer exists in repos module."""
        import agenticore.repos as repos_mod

        assert not hasattr(repos_mod, "_authenticated_url")
