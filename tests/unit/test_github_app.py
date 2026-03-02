"""Unit tests for agenticore.github_app — GitHub App JWT + installation token exchange."""

import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from agenticore.config import reset_config
from agenticore.github_app import GitHubAppAuth, get_github_app_auth, reset_github_app_auth


@pytest.fixture(scope="module")
def test_pem():
    """Generate a valid RSA private key PEM for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    reset_github_app_auth()
    yield
    reset_config()
    reset_github_app_auth()


@pytest.mark.unit
class TestGitHubAppAuth:
    def test_enabled_all_set(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "456")
        assert auth.enabled is True

    def test_not_enabled_missing_app_id(self, test_pem):
        auth = GitHubAppAuth("", test_pem, "456")
        assert auth.enabled is False

    def test_not_enabled_missing_key(self):
        auth = GitHubAppAuth("123", "", "456")
        assert auth.enabled is False

    def test_not_enabled_missing_installation_id(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "")
        assert auth.enabled is False

    def test_generate_jwt_claims(self, test_pem):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        auth = GitHubAppAuth("42", test_pem, "99")
        token = auth._generate_jwt()
        # Extract public key from private key for verification
        private_key = load_pem_private_key(test_pem.encode(), password=None)
        public_key = private_key.public_key()
        payload = pyjwt.decode(token, public_key, algorithms=["RS256"])
        assert payload["iss"] == "42"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]

    def test_get_token_returns_cached(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "456")
        auth._cached_token = "ghs_cached123"
        auth._token_expires_at = time.time() + 3600
        assert auth.get_token() == "ghs_cached123"

    def test_get_token_refreshes_near_expiry(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "456")
        auth._cached_token = "ghs_old"
        auth._token_expires_at = time.time() + 60  # within 5-min buffer

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "token": "ghs_new",
            "expires_at": "2099-01-01T00:00:00Z",
        }

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with patch("agenticore.github_app.httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = auth.get_token()

        assert result == "ghs_new"

    def test_get_token_returns_none_on_http_error(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "456")

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Bad credentials"

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with patch("agenticore.github_app.httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = auth.get_token()

        assert result is None

    def test_get_token_returns_none_on_network_error(self, test_pem):
        auth = GitHubAppAuth("123", test_pem, "456")

        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("connection refused")

        with patch("agenticore.github_app.httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = auth.get_token()

        assert result is None

    def test_get_token_returns_none_when_disabled(self):
        auth = GitHubAppAuth("", "", "")
        assert auth.get_token() is None


@pytest.mark.unit
class TestSingleton:
    def test_get_github_app_auth_returns_same_instance(self):
        with patch.dict(
            "os.environ",
            {"GITHUB_APP_ID": "", "GITHUB_APP_INSTALLATION_ID": "", "GITHUB_APP_PRIVATE_KEY_PATH": ""},
        ):
            a = get_github_app_auth()
            b = get_github_app_auth()
        assert a is b

    def test_reset_clears_instance(self):
        with patch.dict(
            "os.environ",
            {"GITHUB_APP_ID": "", "GITHUB_APP_INSTALLATION_ID": "", "GITHUB_APP_PRIVATE_KEY_PATH": ""},
        ):
            a = get_github_app_auth()
            reset_github_app_auth()
            b = get_github_app_auth()
        assert a is not b
