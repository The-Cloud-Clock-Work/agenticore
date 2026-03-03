"""Unit tests for AgentModeConfig in config module."""

import os
from unittest.mock import patch

import pytest

from agenticore.config import load_config, reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


_AGENT_CLEAN_ENV = {
    "AGENT_MODE": "",
    "AGENT_MODE_PACKAGE_DIR": "",
    "AGENT_MODE_EVALUATION_DIR": "",
    "PACKAGE_REPO_URL": "",
    "PACKAGE_REPO_BRANCH": "",
    "AGENT_MODE_MODEL": "",
    "AGENT_MODE_MAX_TURNS": "",
    "AGENT_MODE_PERMISSION_MODE": "",
    "AGENT_MODE_OUTPUT_FORMAT": "",
    "AGENT_MODE_EFFORT": "",
    "AGENT_MODE_TIMEOUT": "",
    "AGENT_MODE_MAX_RETRIES": "",
    "AGENT_MODE_SESSION_TTL": "",
    "AGENT_MODE_APPEND_SYSTEM_PROMPT": "",
    "REDIS_URL": "",
}


@pytest.mark.unit
class TestAgentModeConfig:
    @patch.dict(os.environ, _AGENT_CLEAN_ENV)
    def test_defaults(self, tmp_path):
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.agent_mode.enabled is False
        assert cfg.agent_mode.package_dir == "/app/package"
        assert cfg.agent_mode.evaluation_dir == "/app/evaluation"
        assert cfg.agent_mode.repo_url == ""
        assert cfg.agent_mode.repo_branch == "main"
        assert cfg.agent_mode.model == "sonnet"
        assert cfg.agent_mode.max_turns == 80
        assert cfg.agent_mode.permission_mode == "bypassPermissions"
        assert cfg.agent_mode.output_format == "json"
        assert cfg.agent_mode.effort == ""
        assert cfg.agent_mode.timeout == 3600
        assert cfg.agent_mode.max_retry_attempts == 3
        assert cfg.agent_mode.session_ttl == 86400
        assert cfg.agent_mode.append_system_prompt is True

    @patch.dict(
        os.environ,
        {
            **_AGENT_CLEAN_ENV,
            "AGENT_MODE": "true",
            "AGENT_MODE_PACKAGE_DIR": "/custom/pkg",
            "PACKAGE_REPO_URL": "https://github.com/org/agent.git",
            "AGENT_MODE_MODEL": "opus",
            "AGENT_MODE_MAX_TURNS": "50",
            "AGENT_MODE_TIMEOUT": "7200",
            "AGENT_MODE_APPEND_SYSTEM_PROMPT": "false",
        },
    )
    def test_env_overrides(self, tmp_path):
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.agent_mode.enabled is True
        assert cfg.agent_mode.package_dir == "/custom/pkg"
        assert cfg.agent_mode.repo_url == "https://github.com/org/agent.git"
        assert cfg.agent_mode.model == "opus"
        assert cfg.agent_mode.max_turns == 50
        assert cfg.agent_mode.timeout == 7200
        assert cfg.agent_mode.append_system_prompt is False

    @patch.dict(os.environ, {**_AGENT_CLEAN_ENV, "AGENT_MODE": "false"})
    def test_disabled_explicitly(self, tmp_path):
        cfg = load_config(str(tmp_path / "x.yml"))
        assert cfg.agent_mode.enabled is False
