"""Unit tests for runner module."""

import os
from unittest.mock import patch

import pytest

from agenticore.config import reset_config
from agenticore.runner import _build_otel_env


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
