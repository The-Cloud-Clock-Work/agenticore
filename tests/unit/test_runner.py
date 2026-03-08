"""Unit tests for runner module."""

import json
import os
from unittest.mock import patch

import pytest

from agenticore.config import reset_config
from agenticore.runner import _build_env, _build_otel_env


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


# Patch _write_oauth_credentials globally — tests shouldn't write real files
_WRITE_CREDS_PATCH = "agenticore.runner._write_oauth_credentials"


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
    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_sets_oauth_token_raw_string(self):
        """CLAUDE_CODE_OAUTH_TOKEN must be the raw access token string."""
        token_data = {
            "token": "sk-ant-oat01-long-oauth-token-value",
            "refresh_token": "refresh-abc",
            "expires_at": 1750000000,
            "scope": "user:inference user:profile",
        }
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch(_WRITE_CREDS_PATCH) as mock_write,
        ):
            env = _build_env()
        # Raw token string, not JSON
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-long-oauth-token-value"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        # Credentials file written with full OAuth data
        mock_write.assert_called_once()
        args = mock_write.call_args
        assert args[0][0] == "sk-ant-oat01-long-oauth-token-value"
        assert args[0][1] == "refresh-abc"
        assert args[0][2] == 1750000000

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_resolve_github_token_sets_env(self):
        token_data = {"token": "sk-ant-oat01-long-oauth-token-value", "refresh_token": "", "expires_at": 0, "scope": ""}
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch("agenticore.runner.resolve_github_token", return_value="gh-tok"),
            patch(_WRITE_CREDS_PATCH),
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
        # Set a GITHUB_TOKEN in os.environ to verify it gets removed
        with patch.dict(os.environ, {"GITHUB_TOKEN": "should-be-removed"}, clear=False):
            with patch("agenticore.runner.resolve_github_token", return_value=None):
                env = _build_env()
        assert "GITHUB_TOKEN" not in env

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "http://broker",
            "AUTH_BROKER_API_KEY": "key",
            "ANTHROPIC_AUTH_TOKEN": "existing-key",
        },
        clear=False,
    )
    def test_broker_returns_none_leaves_env_key(self):
        with patch("agenticore.runner._fetch_full_token_from_auth_broker", return_value=None):
            env = _build_env()
        # Broker returned None → ANTHROPIC_AUTH_TOKEN from os.environ preserved
        assert env["ANTHROPIC_AUTH_TOKEN"] == "existing-key"

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "http://broker",
            "AUTH_BROKER_API_KEY": "key",
            "ANTHROPIC_AUTH_TOKEN": "existing-key",
        },
        clear=False,
    )
    def test_broker_connect_error_preserves_env_key(self):
        with patch(
            "agenticore.runner._fetch_full_token_from_auth_broker",
            side_effect=None,
            return_value=None,
        ):
            env = _build_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "existing-key"

    def test_broker_clears_anthropic_base_url_and_auth_token(self):
        """When broker token received, ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN must be removed."""
        token_data = {
            "token": "sk-ant-oat01-long-oauth-token-value",
            "refresh_token": "refresh-abc",
            "expires_at": 1750000000,
            "scope": "user:inference",
        }
        with patch.dict(
            os.environ,
            {
                "AUTH_BROKER_URL": "http://broker",
                "AUTH_BROKER_API_KEY": "key",
                "ANTHROPIC_BASE_URL": "http://litellm-proxy:4000",
                "ANTHROPIC_AUTH_TOKEN": "old-static-key",
            },
            clear=False,
        ):
            with (
                patch(
                    "agenticore.runner._fetch_full_token_from_auth_broker",
                    side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
                ),
                patch(_WRITE_CREDS_PATCH),
            ):
                env = _build_env()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-long-oauth-token-value"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env

    def test_broker_failure_warns(self, caplog):
        """Broker configured but returns None → warning logged, static env preserved."""
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
        assert env.get("ANTHROPIC_BASE_URL") == "http://litellm:4000"

    def test_cf_headers_auto_build(self):
        """CF_ACCESS_CLIENT_ID + SECRET → ANTHROPIC_CUSTOM_HEADERS set automatically."""
        import json

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
        token_data = {"token": "sk-ant-oat01-long-oauth-token-value", "refresh_token": "", "expires_at": 0, "scope": ""}
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch(
                "agenticore.runner._fetch_from_auth_broker",
                side_effect=lambda svc, **kw: "ya29.google-token-value" if svc == "google" else None,
            ),
            patch(_WRITE_CREDS_PATCH),
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
        token_data = {"token": "sk-ant-oat01-long-oauth-token-value", "refresh_token": "", "expires_at": 0, "scope": ""}
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch(
                "agenticore.runner._fetch_from_auth_broker",
                side_effect=lambda svc, **kw: None,
            ),
            patch(_WRITE_CREDS_PATCH),
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
        token_data = {"token": "short", "refresh_token": "", "expires_at": 0, "scope": ""}
        with patch(
            "agenticore.runner._fetch_full_token_from_auth_broker",
            side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
        ):
            with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "static-key"}, clear=False):
                env = _build_env()
        # Short token rejected → static fallback preserved
        assert env["ANTHROPIC_AUTH_TOKEN"] == "static-key"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_nested_token_dict(self):
        """Handle Auth Broker returning token as nested dict."""
        token_data = {
            "token": {"token": "sk-ant-oat01-nested-token-value", "access_token": "fallback"},
            "refresh_token": "",
            "expires_at": 0,
            "scope": "",
        }
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch(_WRITE_CREDS_PATCH),
        ):
            env = _build_env()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-nested-token-value"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_credentials_file_written(self):
        """Verify _write_oauth_credentials is called with correct args."""
        token_data = {
            "token": "sk-ant-oat01-abcdef1234567890",
            "refresh_token": "sk-ant-ort01-refresh-token",
            "expires_at": 1773038462,
            "scope": "user:inference user:profile user:sessions:claude_code user:mcp_servers",
        }
        with (
            patch(
                "agenticore.runner._fetch_full_token_from_auth_broker",
                side_effect=lambda svc, **kw: token_data if svc == "anthropic" else None,
            ),
            patch(_WRITE_CREDS_PATCH) as mock_write,
        ):
            _build_env()
        mock_write.assert_called_once()
        args, kwargs = mock_write.call_args
        assert args[0] == "sk-ant-oat01-abcdef1234567890"
        assert args[1] == "sk-ant-ort01-refresh-token"
        assert args[2] == 1773038462
        assert args[3] == "user:inference user:profile user:sessions:claude_code user:mcp_servers"

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
        # _fetch_from_auth_broker should never be called when broker is not configured
        mock_fetch.assert_not_called()
        assert "GOOGLE_AUTH_TOKEN" not in env


@pytest.mark.unit
class TestWriteOAuthCredentials:
    def test_writes_credentials_json(self, tmp_path):
        from agenticore.runner import _write_oauth_credentials

        _write_oauth_credentials(
            "sk-ant-oat01-test-access-token",
            "sk-ant-ort01-test-refresh-token",
            1773038462,
            "user:inference user:profile",
            claude_home=str(tmp_path),
        )
        creds_file = tmp_path / ".credentials.json"
        assert creds_file.exists()
        creds = json.loads(creds_file.read_text())
        oauth = creds["claudeAiOauth"]
        assert oauth["accessToken"] == "sk-ant-oat01-test-access-token"
        assert oauth["refreshToken"] == "sk-ant-ort01-test-refresh-token"
        assert oauth["expiresAt"] == 1773038462000  # seconds → milliseconds
        assert oauth["scopes"] == ["user:inference", "user:profile"]

    def test_preserves_existing_keys(self, tmp_path):
        from agenticore.runner import _write_oauth_credentials

        creds_file = tmp_path / ".credentials.json"
        creds_file.write_text(json.dumps({"otherKey": "preserved"}))

        _write_oauth_credentials(
            "sk-ant-oat01-test",
            "",
            0,
            "",
            claude_home=str(tmp_path),
        )
        creds = json.loads(creds_file.read_text())
        assert creds["otherKey"] == "preserved"
        assert "claudeAiOauth" in creds
