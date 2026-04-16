"""Unit tests for agent_mode/agent.py."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenticore.agent_mode.agent import (
    AgentExecutor,
    _load_system_prompt,
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


# ── build_claude_cmd ─────────────────────────────────────────────────────


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
        cmd = build_claude_cmd("continue", claude_session_id="sid-2", stateless=False, resume=True)
        assert "--resume" in cmd
        assert "sid-2" in cmd
        assert "--session-id" not in cmd
        assert "--no-session-persistence" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_first_turn_persistent(self):
        cmd = build_claude_cmd("hello", claude_session_id="sid-3", stateless=False, resume=False)
        assert "--session-id" in cmd
        assert "sid-3" in cmd
        assert "--no-session-persistence" not in cmd
        assert "--resume" not in cmd

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
    def test_inline_prompt_overrides_system_md(self, tmp_path):
        """Inline system_prompt takes priority over system.md file."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "system.md").write_text("From file.")
        with patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": str(pkg_dir)}):
            reset_config()
            cmd = build_claude_cmd("task", system_prompt="Inline override.")
            assert "--system-prompt" in cmd
            assert "--append-system-prompt-file" not in cmd
            assert "--system-prompt-file" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_system_prompt_when_no_file(self):
        """No system prompt flags when system.md doesn't exist and none provided."""
        cmd = build_claude_cmd("task")
        assert "--system-prompt" not in cmd
        assert "--system-prompt-file" not in cmd
        assert "--append-system-prompt-file" not in cmd

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
    def test_disallowed_tools(self):
        cmd = build_claude_cmd("task", disallowed_tools="Bash,Write")
        assert "--disallowedTools" in cmd
        assert "Bash" in cmd
        assert "Write" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_fallback_model(self):
        cmd = build_claude_cmd("task", fallback_model="haiku")
        assert "--fallback-model" in cmd
        assert "haiku" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_effort_when_empty(self):
        cmd = build_claude_cmd("task")
        assert "--effort" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_budget_when_zero(self):
        cmd = build_claude_cmd("task", max_budget_usd=0)
        assert "--max-budget-usd" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_fallback_when_empty(self):
        cmd = build_claude_cmd("task")
        assert "--fallback-model" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_no_session_flags_without_id(self):
        """When no session ID and not stateless, no session flags added."""
        cmd = build_claude_cmd("task", claude_session_id="", stateless=False)
        assert "--resume" not in cmd
        assert "--session-id" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_max_turns_override(self):
        cmd = build_claude_cmd("task", max_turns=25)
        assert "25" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_permission_mode_override(self):
        cmd = build_claude_cmd("task", permission_mode="plan")
        assert "plan" in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_output_format_override(self):
        cmd = build_claude_cmd("task", output_format="text")
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "text"

    def test_stream_json_adds_partial_message_flags(self):
        cmd = build_claude_cmd("task", output_format="stream-json")
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd

    def test_non_stream_json_no_partial_flags(self):
        cmd = build_claude_cmd("task", output_format="json")
        assert "--include-partial-messages" not in cmd

    @patch.dict(os.environ, {**_BASE_ENV, "AGENT_MODE_EFFORT": "medium"})
    def test_config_effort_default(self):
        """Config-level effort used when no per-request override."""
        reset_config()
        cmd = build_claude_cmd("task")
        assert "--effort" in cmd
        assert "medium" in cmd

    @patch.dict(os.environ, {**_BASE_ENV, "AGENT_MODE_EFFORT": "medium"})
    def test_request_effort_overrides_config(self):
        """Per-request effort overrides config default."""
        reset_config()
        cmd = build_claude_cmd("task", effort="high")
        assert "high" in cmd
        assert "medium" not in cmd

    @patch.dict(os.environ, _BASE_ENV)
    def test_message_always_last(self):
        """The message is always the last element of the command."""
        cmd = build_claude_cmd(
            "my task",
            claude_session_id="s",
            stateless=True,
            model="opus",
            effort="high",
            max_budget_usd=10.0,
        )
        assert cmd[-1] == "my task"

    @patch.dict(os.environ, _BASE_ENV)
    def test_allowed_tools_strips_whitespace(self):
        cmd = build_claude_cmd("task", allowed_tools=" Read , Grep , ")
        tools_start = cmd.index("--allowedTools") + 1
        # Extract tools until next flag or message
        tools = []
        for i in range(tools_start, len(cmd)):
            if cmd[i].startswith("--") or cmd[i] == "task":
                break
            tools.append(cmd[i])
        assert "Read" in tools
        assert "Grep" in tools
        assert " Read " not in tools

    @patch.dict(os.environ, _BASE_ENV)
    def test_empty_message(self):
        """Empty message string should not crash — becomes last element."""
        cmd = build_claude_cmd("")
        assert cmd[-1] == ""

    @patch.dict(os.environ, _BASE_ENV)
    def test_allowed_tools_all_empty_after_split(self):
        """When allowed_tools is only commas, --allowedTools is omitted entirely."""
        cmd = build_claude_cmd("task", allowed_tools=",")
        assert "--allowedTools" not in cmd


# ── digest_claude_output ──────────────────────────────────────────────────


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
        assert "Empty output" in result["result"]

    def test_whitespace_only_output(self):
        result = digest_claude_output("   \n\n  ")
        assert result["is_error"] is True

    def test_none_like_output(self):
        result = digest_claude_output(None)
        assert result["is_error"] is True

    def test_plain_text_fallback(self):
        result = digest_claude_output("Just plain text output")
        assert result["result"] == "Just plain text output"

    def test_last_json_line(self):
        output = 'some log line\n{"result": "ok", "session_id": "s1"}\n'
        result = digest_claude_output(output)
        assert result["result"] == "ok"
        assert result["session_id"] == "s1"

    def test_session_id_camel_case(self):
        """sessionId (camelCase) is also recognized."""
        data = {"result": "ok", "sessionId": "camel-id"}
        result = digest_claude_output(json.dumps(data))
        assert result["session_id"] == "camel-id"

    def test_non_dict_blocks_skipped(self):
        """Non-dict items in result list are skipped gracefully."""
        data = {
            "result": [
                "just a string",
                42,
                {"type": "text", "text": "real text"},
                None,
            ],
        }
        result = digest_claude_output(json.dumps(data))
        assert result["result"] == "real text"

    def test_numeric_result_converted_to_str(self):
        data = {"result": 42}
        result = digest_claude_output(json.dumps(data))
        assert result["result"] == "42"

    def test_is_error_true_from_data(self):
        data = {"result": "failed", "is_error": True}
        result = digest_claude_output(json.dumps(data))
        assert result["is_error"] is True

    def test_usage_field_extracted(self):
        data = {"result": "ok", "usage": {"input_tokens": 100, "output_tokens": 50}}
        result = digest_claude_output(json.dumps(data))
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50

    def test_multiple_json_lines_uses_last_valid(self):
        """When full parse fails and we scan lines, last valid JSON line wins."""
        output = 'log line\n{"result": "first"}\n{"result": "second", "session_id": "s2"}\n'
        result = digest_claude_output(output)
        # Reverse scan finds "second" first
        assert result["result"] == "second"
        assert result["session_id"] == "s2"

    def test_malformed_json_lines_skipped(self):
        output = 'not json\n{bad json{\n{"result": "found"}\nmore text\n'
        result = digest_claude_output(output)
        assert result["result"] == "found"

    def test_no_json_in_any_line(self):
        """When no line contains valid JSON, raw text is returned as result."""
        output = "line one\nline two\nno json here"
        result = digest_claude_output(output)
        assert result["result"] == output.strip()
        assert result["is_error"] is False

    def test_all_json_lines_malformed(self):
        """When all JSON-looking lines are malformed, raw text fallback is used."""
        output = "{bad json\n{also bad\n{still bad"
        result = digest_claude_output(output)
        assert result["result"] == output.strip()
        assert result["session_id"] == ""

    def test_empty_result_list(self):
        data = {"result": []}
        result = digest_claude_output(json.dumps(data))
        assert result["result"] == ""
        assert result["tool_uses"] == []

    def test_tool_use_input_preserved(self):
        data = {
            "result": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "timeout": 30}},
            ],
        }
        result = digest_claude_output(json.dumps(data))
        assert result["tool_uses"][0]["input"]["command"] == "ls -la"


# ── _load_system_prompt ──────────────────────────────────────────────────


@pytest.mark.unit
class TestLoadSystemPrompt:
    def test_loads_existing_file(self, tmp_path):
        (tmp_path / "system.md").write_text("  You are helpful.  ")
        result = _load_system_prompt(str(tmp_path))
        assert result == "You are helpful."

    def test_returns_none_when_missing(self, tmp_path):
        result = _load_system_prompt(str(tmp_path))
        assert result is None

    def test_caches_after_first_read(self, tmp_path):
        (tmp_path / "system.md").write_text("cached")
        result1 = _load_system_prompt(str(tmp_path))
        # Modify the file — should NOT be re-read
        (tmp_path / "system.md").write_text("modified")
        result2 = _load_system_prompt(str(tmp_path))
        assert result1 == result2 == "cached"

    def test_reset_clears_cache(self, tmp_path):
        (tmp_path / "system.md").write_text("v1")
        _load_system_prompt(str(tmp_path))
        reset_system_prompt_cache()
        (tmp_path / "system.md").write_text("v2")
        result = _load_system_prompt(str(tmp_path))
        assert result == "v2"


# ── AgentExecutor ─────────────────────────────────────────────────────────


def _mock_subprocess(stdout_data: str, returncode: int = 0, stderr_data: str = ""):
    """Create a mock for asyncio.create_subprocess_exec."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(stdout_data.encode(), stderr_data.encode()))
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    return mock_proc


@pytest.fixture()
def _no_redis():
    from agenticore.jobs import _reset_redis

    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
        _reset_redis()
        yield
    _reset_redis()


@pytest.fixture()
def _agent_env(tmp_path, _no_redis):
    """Set up agent mode env pointing to a real temp package dir."""
    pkg_dir = tmp_path / "package"
    pkg_dir.mkdir()
    (pkg_dir / "CLAUDE.md").write_text("# Test")

    sessions_file = tmp_path / "sessions.json"
    state_file = tmp_path / "state.json"

    env = {
        **_BASE_ENV,
        "AGENT_MODE_PACKAGE_DIR": str(pkg_dir),
        "AGENT_MODE_MAX_RETRIES": "1",
        "AGENT_MODE_TIMEOUT": "10",
    }
    with (
        patch.dict(os.environ, env),
        patch("agenticore.agent_mode.session_registry._sessions_file", return_value=sessions_file),
        patch("agenticore.agent_mode.state._state_file", return_value=state_file),
    ):
        reset_config()
        reset_system_prompt_cache()
        yield pkg_dir


@pytest.mark.unit
class TestAgentExecutor:
    @pytest.mark.asyncio
    async def test_execute_success(self, _agent_env):
        """Successful execution returns parsed result."""
        output = json.dumps({"result": "Done!", "session_id": "s1", "cost_usd": 0.02, "num_turns": 3})
        mock_proc = _mock_subprocess(output)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="hello", external_uuid="test-uuid-1", wait=True)

        assert result["result"] == "Done!"
        assert result["session_id"] == "s1"
        assert result["is_error"] is False
        assert result["cost_usd"] == 0.02
        assert result["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_execute_failure_nonzero_exit(self, _agent_env):
        """Non-zero exit code sets is_error=True."""
        mock_proc = _mock_subprocess('{"result": ""}', returncode=1, stderr_data="Claude crashed")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="fail", external_uuid="test-uuid-2", wait=True)

        assert result["is_error"] is True

    @pytest.mark.asyncio
    async def test_execute_sets_env_vars(self, _agent_env):
        """Correlation ID and Claude session ID are set in subprocess env."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        captured_env = {}

        async def capture_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="corr-id-123", wait=True)

        assert captured_env["AGENTICORE_CORRELATION_ID"] == "corr-id-123"
        assert "AGENTICORE_CLAUDE_SESSION_ID" in captured_env

    @pytest.mark.asyncio
    async def test_execute_marks_session_complete(self, _agent_env):
        """Successful execution marks session as completed."""
        from agenticore.agent_mode.session_registry import resolve_external_uuid

        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="complete-uuid", wait=True)

        mapping = resolve_external_uuid("complete-uuid")
        assert mapping is not None
        assert mapping.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_marks_session_failed(self, _agent_env):
        """Failed execution marks session as failed."""
        from agenticore.agent_mode.session_registry import resolve_external_uuid

        mock_proc = _mock_subprocess("", returncode=1, stderr_data="error")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            await executor.execute(message="fail", external_uuid="fail-uuid", wait=True)

        mapping = resolve_external_uuid("fail-uuid")
        assert mapping is not None
        assert mapping.status == "failed"

    @pytest.mark.asyncio
    async def test_execute_stateless_uses_fresh_session(self, _agent_env):
        """Stateless calls get fresh session IDs (not the external UUID)."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        captured_cmds = []

        async def capture_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="ext-uuid", stateless=True, wait=True)

        cmd = captured_cmds[0]
        assert "--session-id" in cmd
        assert "--no-session-persistence" in cmd
        # The session ID should NOT be the external UUID for stateless
        sid_idx = cmd.index("--session-id") + 1
        assert cmd[sid_idx] != "ext-uuid"

    @pytest.mark.asyncio
    async def test_execute_persistent_first_call_no_resume(self, _agent_env):
        """First persistent call has no --resume (empty claude_session_id)."""
        output = json.dumps({"result": "ok", "session_id": "real-sid"})
        mock_proc = _mock_subprocess(output)

        captured_cmds = []

        async def capture_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="persist-uuid", stateless=False, wait=True)

        cmd = captured_cmds[0]
        assert "--resume" not in cmd
        assert "--session-id" not in cmd

    @pytest.mark.asyncio
    async def test_execute_with_context_via_stdin(self, _agent_env):
        """Context dict is passed via stdin as JSON."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        captured_stdin = []

        async def capture_exec(*args, **kwargs):
            if kwargs.get("stdin") is not None:
                captured_stdin.append("pipe")
            return mock_proc

        original_communicate = mock_proc.communicate

        async def capture_communicate(input=None):
            if input:
                captured_stdin.append(input)
            return await original_communicate(input=input)

        mock_proc.communicate = capture_communicate

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(
                message="test",
                external_uuid="ctx-uuid",
                wait=True,
                context={"key": "value"},
            )

        assert len(captured_stdin) > 0

    @pytest.mark.asyncio
    async def test_execute_cwd_is_package_dir(self, _agent_env):
        """Subprocess runs in the package directory."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        captured_cwd = []

        async def capture_exec(*args, **kwargs):
            captured_cwd.append(kwargs.get("cwd"))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="cwd-uuid", wait=True)

        assert captured_cwd[0] == str(_agent_env)

    @pytest.mark.asyncio
    async def test_execute_all_retries_exhausted(self, _agent_env):
        """When all retries are exhausted, returns error."""
        with (
            patch.dict(os.environ, {"AGENT_MODE_MAX_RETRIES": "2"}),
            patch("asyncio.create_subprocess_exec", side_effect=RuntimeError("boom")),
        ):
            reset_config()
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="retry-exhaust", wait=True)

        assert result["is_error"] is True
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_timeout(self, _agent_env):
        """Subprocess timeout produces error result."""
        with (
            patch.dict(os.environ, {"AGENT_MODE_TIMEOUT": "1", "AGENT_MODE_MAX_RETRIES": "1"}),
            patch("asyncio.create_subprocess_exec", side_effect=asyncio.TimeoutError()),
        ):
            reset_config()
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="timeout-uuid", wait=True)

        assert result["is_error"] is True
        assert "Timeout" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_execute_saves_state(self, _agent_env, tmp_path):
        """State is saved for hooks integration."""
        from agenticore.agent_mode.state import get_session_state

        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            await executor.execute(
                message="test",
                external_uuid="state-uuid",
                wait=True,
                meta={"platform": "test"},
            )

        state = get_session_state("state-uuid")
        assert state is not None
        assert state["uuid"] == "state-uuid"
        assert state["meta"]["platform"] == "test"

    @pytest.mark.asyncio
    async def test_execute_strips_internal_stderr(self, _agent_env):
        """_stderr is removed from the final result dict."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output, stderr_data="some internal warning")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="strip-uuid", wait=True)

        assert "_stderr" not in result

    @pytest.mark.asyncio
    async def test_execute_package_dir_not_exists_falls_back_to_cwd(self, _agent_env):
        """When package_dir doesn't exist, cwd fallback is used."""
        output = json.dumps({"result": "ok"})
        mock_proc = _mock_subprocess(output)

        captured_cwd = []

        async def capture_exec(*args, **kwargs):
            captured_cwd.append(kwargs.get("cwd"))
            return mock_proc

        with (
            patch.dict(os.environ, {"AGENT_MODE_PACKAGE_DIR": "/nonexistent/pkg/path"}),
            patch("asyncio.create_subprocess_exec", side_effect=capture_exec),
        ):
            reset_config()
            executor = AgentExecutor()
            await executor.execute(message="test", external_uuid="fallback-cwd", wait=True)

        from pathlib import Path

        assert captured_cwd[0] == str(Path.cwd())

    @pytest.mark.asyncio
    async def test_execute_retryable_error_triggers_retry(self, _agent_env):
        """Retryable error triggers retry with backoff."""
        error_output = json.dumps({"result": "rate limit exceeded", "is_error": True})
        success_output = json.dumps({"result": "Done!", "is_error": False})
        error_proc = _mock_subprocess(error_output, returncode=1, stderr_data="429 too many requests")
        success_proc = _mock_subprocess(success_output)

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return error_proc
            return success_proc

        with (
            patch.dict(os.environ, {"AGENT_MODE_MAX_RETRIES": "3"}),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            reset_config()
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="retry-ok", wait=True)

        assert call_count == 2
        assert result["result"] == "Done!"
        assert result["is_error"] is False

    @pytest.mark.asyncio
    async def test_execute_non_retryable_error_no_retry(self, _agent_env):
        """Non-retryable error returns immediately without retry."""
        error_output = json.dumps({"result": "unknown internal error", "is_error": True})
        error_proc = _mock_subprocess(error_output, returncode=1, stderr_data="segfault")

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return error_proc

        with (
            patch.dict(os.environ, {"AGENT_MODE_MAX_RETRIES": "3"}),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec),
        ):
            reset_config()
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="no-retry", wait=True)

        assert call_count == 1
        assert result["is_error"] is True

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_with_valid_json_stdout(self, _agent_env):
        """Non-zero exit with parseable JSON stdout preserves is_error and result."""
        output = json.dumps({"result": "partial output before crash", "is_error": False})
        mock_proc = _mock_subprocess(output, returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="exit-json", wait=True)

        assert result["is_error"] is True
        assert result["result"] == "partial output before crash"

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_empty_result_uses_stderr(self, _agent_env):
        """Non-zero exit with empty result falls back to stderr."""
        output = json.dumps({"result": ""})
        mock_proc = _mock_subprocess(output, returncode=1, stderr_data="fatal crash")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="exit-stderr", wait=True)

        assert result["is_error"] is True
        assert result["result"] == "fatal crash"

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_no_stderr_uses_exit_code(self, _agent_env):
        """Non-zero exit with no stderr uses exit code message."""
        output = json.dumps({"result": ""})
        mock_proc = _mock_subprocess(output, returncode=137, stderr_data="")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="exit-code", wait=True)

        assert result["is_error"] is True
        assert "137" in result["result"]

    @pytest.mark.asyncio
    async def test_execute_build_env_raises(self, _agent_env):
        """When build_subprocess_env raises, exception propagates (not inside retry loop)."""
        with patch("agenticore.runner.build_subprocess_env", side_effect=RuntimeError("env build failed")):
            executor = AgentExecutor()
            with pytest.raises(RuntimeError, match="env build failed"):
                await executor.execute(message="test", external_uuid="env-fail", wait=True)

    @pytest.mark.asyncio
    async def test_execute_communicate_timeout(self, _agent_env):
        """Timeout during proc.communicate produces error result."""
        mock_proc = AsyncMock()
        mock_proc.pid = 99999

        async def timeout_communicate(input=None):
            raise TimeoutError("deadline exceeded")

        mock_proc.communicate = timeout_communicate

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="test", external_uuid="comm-timeout", wait=True)

        assert result["is_error"] is True

    @pytest.mark.asyncio
    async def test_persistent_first_call_captures_session_id(self, _agent_env):
        """First persistent call captures real Claude session ID in registry."""
        from agenticore.agent_mode.session_registry import resolve_external_uuid

        output = json.dumps({"result": "Hello!", "session_id": "real-claude-sid"})
        mock_proc = _mock_subprocess(output)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            executor = AgentExecutor()
            result = await executor.execute(message="hi", external_uuid="capture-uuid", stateless=False, wait=True)

        assert result["session_id"] == "real-claude-sid"
        mapping = resolve_external_uuid("capture-uuid")
        assert mapping.claude_session_id == "real-claude-sid"

    @pytest.mark.asyncio
    async def test_persistent_second_call_uses_resume(self, _agent_env):
        """Second persistent call uses --resume with real Claude session ID."""
        from agenticore.agent_mode.session_registry import register_session, update_session_claude_id

        # Pre-populate registry as if first call already happened
        register_session("resume-uuid", stateless=False)
        update_session_claude_id("resume-uuid", "established-sid")

        output = json.dumps({"result": "Continued!", "session_id": "established-sid"})
        mock_proc = _mock_subprocess(output)

        captured_cmds = []

        async def capture_exec(*args, **kwargs):
            captured_cmds.append(list(args))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            executor = AgentExecutor()
            await executor.execute(message="continue", external_uuid="resume-uuid", stateless=False, wait=True)

        cmd = captured_cmds[0]
        assert "--resume" in cmd
        assert "established-sid" in cmd


# ─── execute_streaming stream-json parser tests ──────────────────────────────


class _FakeStdout:
    """Minimal async readline-able stream for mocking proc.stdout."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


def _stream_json_lines(events: list[dict]) -> list[bytes]:
    return [(json.dumps(e) + "\n").encode() for e in events]


def _make_streaming_proc(events: list[dict], returncode: int = 0):
    proc = AsyncMock()
    proc.stdout = _FakeStdout(_stream_json_lines(events))
    proc.returncode = returncode
    proc.pid = 22222
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.stdin = None
    return proc


class TestExecuteStreamingParser:
    """Validates the stream-json → SSE dispatcher in execute_streaming."""

    @pytest.mark.asyncio
    async def test_text_thinking_and_tool_use(self, _agent_env):
        events = [
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "let me think"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'},
                },
            },
            {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 1},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "file1\nfile2"}],
                            "is_error": False,
                        }
                    ]
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "text_delta", "text": "Done."},
                },
            },
            {"type": "result", "subtype": "success", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        proc = _make_streaming_proc(events)

        async def fake_exec(*args, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            executor = AgentExecutor()
            stream_cfg = {"show_thinking": True, "show_tools": True, "show_text": True}
            chunks = []
            async for ch in executor.execute_streaming(
                message="hi",
                external_uuid="stream-test-1",
                stream_cfg=stream_cfg,
            ):
                chunks.append(ch)

        merged = "".join(chunks)
        assert "reasoning_content" in merged
        assert "let me think" in merged
        assert "tool_use: Bash" in merged
        assert "file1" in merged
        assert "Done." in merged
        assert "[DONE]" in merged

    @pytest.mark.asyncio
    async def test_hidden_thinking_filtered(self, _agent_env):
        events = [
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "secret thought"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "visible answer"},
                },
            },
            {"type": "result", "subtype": "success", "usage": {}},
        ]
        proc = _make_streaming_proc(events)

        async def fake_exec(*args, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            executor = AgentExecutor()
            stream_cfg = {"show_thinking": False, "show_tools": False, "show_text": True}
            chunks = []
            async for ch in executor.execute_streaming(
                message="hi",
                external_uuid="stream-test-2",
                stream_cfg=stream_cfg,
            ):
                chunks.append(ch)

        merged = "".join(chunks)
        assert "secret thought" not in merged
        assert "visible answer" in merged
