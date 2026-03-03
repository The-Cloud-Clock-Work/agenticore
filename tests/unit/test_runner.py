"""Unit tests for runner module."""

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
class TestBuildEnvAuthBroker:
    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_sets_ANTHROPIC_AUTH_TOKEN(self):
        with patch(
            "agenticore.runner._fetch_from_auth_broker",
            side_effect=lambda svc, **kw: "sk-ant-123" if svc == "anthropic" else None,
        ):
            env = _build_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ant-123"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_resolve_github_token_sets_env(self):
        with (
            patch(
                "agenticore.runner._fetch_from_auth_broker",
                side_effect=lambda svc, **kw: "sk-ant-123" if svc == "anthropic" else None,
            ),
            patch("agenticore.runner.resolve_github_token", return_value="gh-tok"),
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
        with patch("agenticore.runner._fetch_from_auth_broker", return_value=None):
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
            "agenticore.runner._fetch_from_auth_broker",
            side_effect=None,
            return_value=None,
        ):
            env = _build_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "existing-key"

    def test_broker_clears_anthropic_base_url(self):
        """When broker token received, ANTHROPIC_BASE_URL must be removed (direct Anthropic, not LiteLLM)."""
        with patch.dict(
            os.environ,
            {
                "AUTH_BROKER_URL": "http://broker",
                "AUTH_BROKER_API_KEY": "key",
                "ANTHROPIC_BASE_URL": "http://litellm-proxy:4000",
            },
            clear=False,
        ):
            with patch(
                "agenticore.runner._fetch_from_auth_broker",
                side_effect=lambda svc, **kw: "sk-ant-123" if svc == "anthropic" else None,
            ):
                env = _build_env()
        assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-ant-123"
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
            with patch("agenticore.runner._fetch_from_auth_broker", return_value=None):
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
