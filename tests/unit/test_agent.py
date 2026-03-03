"""Unit tests for agent_mode/agent.py."""

import json
import os
from unittest.mock import patch

import pytest

from agenticore.agent_mode.agent import (
    build_claude_cmd,
    digest_claude_output,
    reset_system_prompt_cache,
)
from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    reset_system_prompt_cache()
    yield
    reset_config()
    reset_system_prompt_cache()


_BASE_ENV = {
    "AGENT_MODE": "true",
    "AGENT_MODE_PACKAGE_DIR": "/tmp/test-pkg",
    "AGENT_MODE_MODEL": "sonnet",
    "AGENT_MODE_MAX_TURNS": "80",
    "AGENT_MODE_PERMISSION_MODE": "bypassPermissions",
    "AGENT_MODE_OUTPUT_FORMAT": "json",
    "AGENT_MODE_EFFORT": "",
    "AGENT_MODE_TIMEOUT": "3600",
    "AGENT_MODE_MAX_RETRIES": "3",
    "AGENT_MODE_SESSION_TTL": "86400",
    "AGENT_MODE_APPEND_SYSTEM_PROMPT": "true",
    "REDIS_URL": "",
}


@pytest.mark.unit
class TestBuildClaudeCmd:
    @patch.dict(os.environ, _BASE_ENV)
    def test_basic_command(self):
        cmd = build_claude_cmd("hello world", claude_session_id="sid-1", stateless=True)
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert "--max-turns" in cmd
        assert "80" in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--session-id" in cmd
        assert "sid-1" in cmd
        assert "--no-session-persistence" in cmd
        assert cmd[-1] == "hello world"

    @patch.dict(os.environ, _BASE_ENV)
    def test_resume_mode(self):
        cmd = build_claude_cmd("continue", claude_session_id="sid-2", stateless=False)
        assert "--resume" in cmd
        assert "sid-2" in cmd
        assert "--session-id" not in cmd
        assert "--no-session-persistence" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_model_override(self):
        cmd = build_claude_cmd("task", model="opus")
        assert "opus" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_inline_system_prompt(self):
        cmd = build_claude_cmd("task", system_prompt="You are a pirate.")
        assert "--system-prompt" in cmd
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == "You are a pirate."

    @patch.dict(os.environ, _BASE_ENV)
    def test_system_md_append_mode(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "system.md").write_text("Be helpful.")
        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg_dir)}):
            reset_config()
            cmd = build_claude_cmd("task", append_system_prompt=True)
            assert "--append-system-prompt-file" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_system_md_replace_mode(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "system.md").write_text("Be helpful.")
        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg_dir)}):
            reset_config()
            cmd = build_claude_cmd("task", append_system_prompt=False)
            assert "--system-prompt-file" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_effort_flag(self):
        cmd = build_claude_cmd("task", effort="high")
        assert "--effort" in cmd
        assert "high" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_budget_flag(self):
        cmd = build_claude_cmd("task", max_budget_usd=5.0)
        assert "--max-budget-usd" in cmd
        assert "5.0" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_allowed_tools(self):
        cmd = build_claude_cmd("task", allowed_tools="Read,Grep,Glob")
        assert "--allowedTools" in cmd
        assert "Read" in cmd
        assert "Grep" in cmd
        assert "Glob" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_effort_when_empty(self):
        cmd = build_claude_cmd("task")
        assert "--effort" not in cmd


@pytest.mark.unit
class TestDigestClaudeOutput:
    def test_json_output(self):
        data = {
            "result": "Hello!",
            "session_id": "sid-123",
            "cost_usd": 0.05,
            "duration_ms": 1500,
            "num_turns": 2,
            "is_error": False,
        }
        result = digest_claude_output(json.dumps(data))
        assert result["result"] == "Hello!"
        assert result["session_id"] == "sid-123"
        assert result["cost_usd"] == 0.05
        assert result["is_error"] is False

    def test_block_list_result(self):
        data = {
            "result": [
                {"type": "text", "text": "First part."},
                {"type": "text", "text": "Second part."},
                {"type": "tool_use", "name": "Read", "input": {"file": "test.py"}},
            ],
            "session_id": "sid-456",
        }
        result = digest_claude_output(json.dumps(data))
        assert "First part." in result["result"]
        assert "Second part." in result["result"]
        assert len(result["tool_uses"]) == 1
        assert result["tool_uses"][0]["name"] == "Read"

    def test_empty_output(self):
        result = digest_claude_output("")
        assert result["is_error"] is True

    def test_plain_text_fallback(self):
        result = digest_claude_output("Just plain text output")
        assert result["result"] == "Just plain text output"

    def test_last_json_line(self):
        output = 'some log line\n{"result": "ok", "session_id": "s1"}\n'
        result = digest_claude_output(output)
        assert result["result"] == "ok"
        assert result["session_id"] == "s1"
