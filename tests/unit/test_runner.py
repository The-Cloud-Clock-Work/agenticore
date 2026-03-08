"""Unit tests for runner module."""

import json
import os
from unittest.mock import patch

import pytest

from agenticore.config import reset_config
from agenticore.runner import _build_env, _build_otel_env

# Patch target for _write_oauth_credentials — tests shouldn't write real files
_WRITE_CREDS = "agenticore.runner._write_oauth_credentials"

# Helper: standard Auth Broker token dict
_BROKER_TOKEN = {
    "token": "sk-ant-oat01-long-oauth-token-value",
    "refresh_token": "sk-ant-ort01-refresh-abc",
    "expires_at": 1773038462,
    "scope": "user:inference user:profile user:sessions:claude_code",
}


def _broker_returns(token_data):
    """Side effect: return token_data for anthropic, None for others."""
    return lambda svc, **kw: token_data if svc == "anthropic" else None


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestBuildOtelEnv:
    @patch.dict(
        os.environ,
        {
            "AGENTICORE_OTEL_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "AGENTICORE_OTEL_LOG_PROMPTS": "false",
            "AGENTICORE_OTEL_LOG_TOOL_DETAILS": "true",
        },
        clear=False,
    )
    def test_otel_env_when_enabled(self):
        env = _build_otel_env()
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env["OTEL_METRICS_EXPORTER"] == "otlp"
        assert env["OTEL_LOGS_EXPORTER"] == "otlp"
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4317"
        assert env["OTEL_LOG_USER_PROMPTS"] == "0"
        assert env["OTEL_LOG_TOOL_DETAILS"] == "1"

    @patch.dict(os.environ, {"AGENTICORE_OTEL_ENABLED": "false"}, clear=False)
    def test_otel_env_when_disabled(self):
        env = _build_otel_env()
        assert env == {}

    @patch.dict(
        os.environ,
        {
            "AGENTICORE_OTEL_ENABLED": "true",
            "AGENTICORE_OTEL_LOG_PROMPTS": "true",
        },
        clear=False,
    )
    def test_otel_log_prompts_enabled(self):
        env = _build_otel_env()
        assert env["OTEL_LOG_USER_PROMPTS"] == "1"


@pytest.mark.unit
class TestBuildEnvAuthBroker:
    """Test _build_env() Anthropic auth: Auth Broker writes credentials.json, removes ANTHROPIC_AUTH_TOKEN."""

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_writes_credentials_and_clears_api_key(self):
        """Auth Broker success → credentials.json written, ANTHROPIC_AUTH_TOKEN removed."""
        with (
            patch("agenticore.runner._fetch_full_token_from_auth_broker", side_effect=_broker_returns(_BROKER_TOKEN)),
            patch(_WRITE_CREDS) as mock_write,
        ):
            env = _build_env()
        # ANTHROPIC_AUTH_TOKEN must NOT be in env (OAuth uses credentials file)
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        # Credentials file written
        mock_write.assert_called_once()
        args = mock_write.call_args[0]
        assert args[0] == "sk-ant-oat01-long-oauth-token-value"
        assert args[1] == "sk-ant-ort01-refresh-abc"
        assert args[2] == 1773038462

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_resolve_github_token_sets_env(self):
        with (
            patch("agenticore.runner._fetch_full_token_from_auth_broker", side_effect=_broker_returns(_BROKER_TOKEN)),
            patch("agenticore.runner.resolve_github_token", return_value="gh-tok"),
            patch(_WRITE_CREDS),
        ):
            env = _build_env()
        assert env["GITHUB_TOKEN"] == "gh-tok"

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "",
            "AUTH_BROKER_API_KEY": "",
            "GITHUB_TOKEN": "static-token",
        },
        clear=False,
    )
    def test_resolve_github_token_static(self):
        with patch("agenticore.runner.resolve_github_token", return_value="static-token"):
            env = _build_env()
        assert env["GITHUB_TOKEN"] == "static-token"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""},
        clear=False,
    )
    def test_no_github_token_removes_from_env(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "should-be-removed"}, clear=False):
            with patch("agenticore.runner.resolve_github_token", return_value=None):
                env = _build_env()
        assert "GITHUB_TOKEN" not in env

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "http://broker",
            "AUTH_BROKER_API_KEY": "key",
            "ANTHROPIC_AUTH_TOKEN": "litellm-key",
            "ANTHROPIC_BASE_URL": "http://litellm:4000",
        },
        clear=False,
    )
    def test_broker_returns_none_preserves_fallback_with_base_url(self):
        """Broker fails + ANTHROPIC_BASE_URL present → keep both as LiteLLM fallback."""
        with patch("agenticore.runner._fetch_full_token_from_auth_broker", return_value=None):
            env = _build_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "litellm-key"
        assert env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"

    def test_broker_returns_none_clears_orphaned_api_key(self):
        """Broker fails + NO ANTHROPIC_BASE_URL → clear ANTHROPIC_AUTH_TOKEN (prevents 401 on api.anthropic.com)."""
        with patch.dict(
            os.environ,
            {
                "AUTH_BROKER_URL": "http://broker",
                "AUTH_BROKER_API_KEY": "key",
                "ANTHROPIC_AUTH_TOKEN": "litellm-key",
            },
            clear=False,
        ):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            with patch("agenticore.runner._fetch_full_token_from_auth_broker", return_value=None):
                env = _build_env()
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_broker_clears_both_env_vars(self):
        """When broker token received, both ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL removed."""
        with patch.dict(
            os.environ,
            {
                "AUTH_BROKER_URL": "http://broker",
                "AUTH_BROKER_API_KEY": "key",
                "ANTHROPIC_BASE_URL": "http://litellm-proxy:4000",
                "ANTHROPIC_AUTH_TOKEN": "old-litellm-key",
            },
            clear=False,
        ):
            with (
                patch(
                    "agenticore.runner._fetch_full_token_from_auth_broker",
                    side_effect=_broker_returns(_BROKER_TOKEN),
                ),
                patch(_WRITE_CREDS),
            ):
                env = _build_env()
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env

    def test_broker_failure_warns(self, caplog):
        """Broker configured but returns None → warning logged."""
        import logging

        with patch.dict(
            os.environ,
            {
                "AUTH_BROKER_URL": "http://broker",
                "AUTH_BROKER_API_KEY": "key",
                "ANTHROPIC_AUTH_TOKEN": "static-key",
                "ANTHROPIC_BASE_URL": "http://litellm:4000",
            },
            clear=False,
        ):
            with patch("agenticore.runner._fetch_full_token_from_auth_broker", return_value=None):
                with caplog.at_level(logging.WARNING, logger="agenticore.runner"):
                    env = _build_env()
        assert "falling back" in caplog.text
        # With ANTHROPIC_BASE_URL present, fallback is valid
        assert env.get("ANTHROPIC_BASE_URL") == "http://litellm:4000"

    def test_cf_headers_auto_build(self):
        """CF_ACCESS_CLIENT_ID + SECRET → ANTHROPIC_CUSTOM_HEADERS set automatically."""
        with patch.dict(
            os.environ,
            {
                "CF_ACCESS_CLIENT_ID": "cf-id",
                "CF_ACCESS_CLIENT_SECRET": "cf-secret",
                "AUTH_BROKER_URL": "",
            },
            clear=False,
        ):
            os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
            env = _build_env()
        headers = json.loads(env["ANTHROPIC_CUSTOM_HEADERS"])
        assert headers["CF-Access-Client-Id"] == "cf-id"
        assert headers["CF-Access-Client-Secret"] == "cf-secret"

    def test_cf_headers_not_overwritten(self):
        """Existing ANTHROPIC_CUSTOM_HEADERS is not overwritten."""
        with patch.dict(
            os.environ,
            {
                "CF_ACCESS_CLIENT_ID": "cf-id",
                "CF_ACCESS_CLIENT_SECRET": "cf-secret",
                "ANTHROPIC_CUSTOM_HEADERS": '{"existing": "value"}',
                "AUTH_BROKER_URL": "",
            },
            clear=False,
        ):
            env = _build_env()
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == '{"existing": "value"}'

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_google_token_sets_env(self):
        """Google token from broker is set as GOOGLE_AUTH_TOKEN."""
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=_broker_returns(_BROKER_TOKEN),
            ),
            patch(
                "agenticore.runner._fetch_from_auth_broker",
                side_effect=lambda svc, **kw: "ya29.google-token-value" if svc == "google" else None,
            ),
            patch(_WRITE_CREDS),
        ):
            env = _build_env()
        assert env["GOOGLE_AUTH_TOKEN"] == "ya29.google-token-value"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_google_none_skips(self):
        """No Google token from broker → GOOGLE_AUTH_TOKEN not set."""
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=_broker_returns(_BROKER_TOKEN),
            ),
            patch("agenticore.runner._fetch_from_auth_broker", side_effect=lambda svc, **kw: None),
            patch(_WRITE_CREDS),
        ):
            env = _build_env()
        assert "GOOGLE_AUTH_TOKEN" not in env

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_short_token_rejected(self):
        """Token shorter than 10 chars is rejected as malformed."""
        short_token = {"token": "short", "refresh_token": "", "expires_at": 0, "scope": ""}
        with patch(
            "agenticore.runner._fetch_full_token_from_auth_broker",
            side_effect=_broker_returns(short_token),
        ):
            with patch.dict(
                os.environ,
                {"ANTHROPIC_AUTH_TOKEN": "static-key", "ANTHROPIC_BASE_URL": "http://litellm:4000"},
                clear=False,
            ):
                env = _build_env()
        # Short token rejected, fallback preserved (has BASE_URL so key stays)
        assert env["ANTHROPIC_AUTH_TOKEN"] == "static-key"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_nested_token_dict(self):
        """Handle Auth Broker returning token as nested dict."""
        nested_token = {
            "token": {"token": "sk-ant-oat01-nested-token-value", "access_token": "fallback"},
            "refresh_token": "",
            "expires_at": 0,
            "scope": "",
        }
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=_broker_returns(nested_token),
            ),
            patch(_WRITE_CREDS) as mock_write,
        ):
            env = _build_env()
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        mock_write.assert_called_once()
        assert mock_write.call_args[0][0] == "sk-ant-oat01-nested-token-value"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""},
        clear=False,
    )
    def test_no_broker_skips_google(self):
        """When broker is not configured, Google token is not attempted."""
        with patch("agenticore.runner._fetch_from_auth_broker") as mock_fetch:
            with patch("agenticore.runner.resolve_github_token", return_value=None):
                env = _build_env()
        mock_fetch.assert_not_called()
        assert "GOOGLE_AUTH_TOKEN" not in env


@pytest.mark.unit
class TestWriteOAuthCredentials:
    """Test _write_oauth_credentials file I/O."""

    def test_writes_credentials_json(self, tmp_path):
        from agenticore.runner import _write_oauth_credentials

        _write_oauth_credentials(
            "sk-ant-oat01-test-access",
            "sk-ant-ort01-test-refresh",
            1773038462,
            "user:inference user:profile",
            claude_home=str(tmp_path),
        )
        creds = json.loads((tmp_path / ".credentials.json").read_text())
        oauth = creds["claudeAiOauth"]
        assert oauth["accessToken"] == "sk-ant-oat01-test-access"
        assert oauth["refreshToken"] == "sk-ant-ort01-test-refresh"
        assert oauth["expiresAt"] == 1773038462000  # seconds → ms
        assert oauth["scopes"] == ["user:inference", "user:profile"]

    def test_preserves_existing_keys(self, tmp_path):
        from agenticore.runner import _write_oauth_credentials

        (tmp_path / ".credentials.json").write_text(json.dumps({"otherKey": "preserved"}))
        _write_oauth_credentials("sk-ant-oat01-test", "", 0, "", claude_home=str(tmp_path))
        creds = json.loads((tmp_path / ".credentials.json").read_text())
        assert creds["otherKey"] == "preserved"
        assert "claudeAiOauth" in creds
