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
    def test_broker_sets_anthropic_api_key(self):
        with patch(
            "agenticore.runner._fetch_from_auth_broker",
            side_effect=lambda svc, **kw: "sk-ant-123" if svc == "anthropic" else None,
        ):
            env = _build_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "http://broker", "AUTH_BROKER_API_KEY": "key"},
        clear=False,
    )
    def test_broker_sets_github_token(self):
        with patch(
            "agenticore.runner._fetch_from_auth_broker",
            side_effect=lambda svc, **kw: "gh-tok" if svc == "github" else None,
        ):
            env = _build_env()
        assert env["GITHUB_TOKEN"] == "gh-tok"

    @patch.dict(
        os.environ,
        {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": "", "GITHUB_TOKEN": "static-token"},
        clear=False,
    )
    def test_broker_disabled_preserves_static_github_token(self):
        env = _build_env()
        assert env["GITHUB_TOKEN"] == "static-token"

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "http://broker",
            "AUTH_BROKER_API_KEY": "key",
            "ANTHROPIC_API_KEY": "existing-key",
        },
        clear=False,
    )
    def test_broker_returns_none_leaves_env_key(self):
        with patch("agenticore.runner._fetch_from_auth_broker", return_value=None):
            env = _build_env()
        # Broker returned None → ANTHROPIC_API_KEY from os.environ preserved
        assert env["ANTHROPIC_API_KEY"] == "existing-key"

    @patch.dict(
        os.environ,
        {
            "AUTH_BROKER_URL": "http://broker",
            "AUTH_BROKER_API_KEY": "key",
            "ANTHROPIC_API_KEY": "existing-key",
        },
        clear=False,
    )
    def test_broker_connect_error_preserves_env_key(self):
        with patch("agenticore.runner._fetch_from_auth_broker", side_effect=None, return_value=None):
            env = _build_env()
        assert env["ANTHROPIC_API_KEY"] == "existing-key"
