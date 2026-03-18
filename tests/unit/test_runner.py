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
class TestBuildEnvAuth:
    def test_oauth_token_clears_api_key_auth(self):
        """CLAUDE_CODE_OAUTH_TOKEN present → removes ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL."""
        with patch.dict(
            os.environ,
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-long-token",
                "ANTHROPIC_AUTH_TOKEN": "old-key",
                "ANTHROPIC_BASE_URL": "http://litellm:4000",
            },
            clear=False,
        ):
            env = _build_env()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-long-token"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env

    def test_no_oauth_token_preserves_static(self):
        """No CLAUDE_CODE_OAUTH_TOKEN → static env preserved."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "static-key",
                "ANTHROPIC_BASE_URL": "http://litellm:4000",
            },
            clear=False,
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env = _build_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "static-key"
        assert env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"

    def test_resolve_github_token_sets_env(self):
        with patch("agenticore.runner.resolve_github_token", return_value="gh-tok"):
            env = _build_env()
        assert env["GITHUB_TOKEN"] == "gh-tok"

    @patch.dict(os.environ, {"GITHUB_TOKEN": "static-token"}, clear=False)
    def test_resolve_github_token_static(self):
        with patch("agenticore.runner.resolve_github_token", return_value="static-token"):
            env = _build_env()
        assert env["GITHUB_TOKEN"] == "static-token"

    def test_no_github_token_removes_from_env(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "should-be-removed"}, clear=False):
            with patch("agenticore.runner.resolve_github_token", return_value=None):
                env = _build_env()
        assert "GITHUB_TOKEN" not in env

    def test_cf_headers_auto_build(self):
        """CF_ACCESS_CLIENT_ID + SECRET → ANTHROPIC_CUSTOM_HEADERS set automatically."""
        with patch.dict(
            os.environ,
            {
                "CF_ACCESS_CLIENT_ID": "cf-id",
                "CF_ACCESS_CLIENT_SECRET": "cf-secret",
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
            },
            clear=False,
        ):
            env = _build_env()
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == '{"existing": "value"}'
