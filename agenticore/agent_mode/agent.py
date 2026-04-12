"""Core agent execution engine.

Builds Claude CLI commands from request parameters and manages execution.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config

_log = logging.getLogger(__name__)

# Cached system prompt content from package/system.md
_cached_system_prompt: Optional[str] = None
_system_prompt_loaded: bool = False


def _load_system_prompt(package_dir: str) -> Optional[str]:
    """Load system.md from package dir if it exists. Cached after first read."""
    global _cached_system_prompt, _system_prompt_loaded
    if _system_prompt_loaded:
        return _cached_system_prompt

    _system_prompt_loaded = True
    path = Path(package_dir) / "system.md"
    if path.exists():
        _cached_system_prompt = path.read_text().strip()
        _log.info("Loaded system prompt from %s (%d chars)", path, len(_cached_system_prompt))
    else:
        _cached_system_prompt = None
        _log.debug("No system.md found at %s", path)
    return _cached_system_prompt


def reset_system_prompt_cache() -> None:
    """Reset the cached system prompt (for testing)."""
    global _cached_system_prompt, _system_prompt_loaded
    _cached_system_prompt = None
    _system_prompt_loaded = False


def build_claude_cmd(
    message: str,
    *,
    claude_session_id: str = "",
    stateless: bool = False,
    model: str = "",
    max_turns: int = 0,
    system_prompt: str = "",
    append_system_prompt: Optional[bool] = None,
    permission_mode: str = "",
    output_format: str = "",
    effort: str = "",
    max_budget_usd: float = 0,
    fallback_model: str = "",
    allowed_tools: str = "",
    disallowed_tools: str = "",
) -> list:
    """Build Claude CLI command from request parameters.

    Each parameter maps directly to a CLI flag. Empty/zero values fall back
    to config defaults.
    """
    cfg = get_config()
    am = cfg.agent_mode

    cmd = [cfg.claude.binary, "-p"]
    cmd.extend(["--output-format", output_format or am.output_format])
    cmd.extend(["--model", model or am.model])
    cmd.extend(["--max-turns", str(max_turns or am.max_turns)])
    cmd.extend(["--permission-mode", permission_mode or am.permission_mode])

    # System prompt resolution
    use_append = append_system_prompt if append_system_prompt is not None else am.append_system_prompt

    if system_prompt:
        # Explicit inline override — always replaces
        cmd.extend(["--system-prompt", system_prompt])
    else:
        # Check for system.md in package dir
        system_md_path = Path(am.package_dir) / "system.md"
        if system_md_path.exists():
            if use_append:
                cmd.extend(["--append-system-prompt-file", str(system_md_path)])
            else:
                cmd.extend(["--system-prompt-file", str(system_md_path)])

    # Optional flags
    resolved_effort = effort or am.effort
    if resolved_effort:
        cmd.extend(["--effort", resolved_effort])
    if max_budget_usd > 0:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if fallback_model:
        cmd.extend(["--fallback-model", fallback_model])
    if allowed_tools:
        cmd.extend(["--allowedTools"] + [t.strip() for t in allowed_tools.split(",") if t.strip()])
    if disallowed_tools:
        cmd.extend(["--disallowedTools"] + [t.strip() for t in disallowed_tools.split(",") if t.strip()])

    # Session handling
    if stateless:
        cmd.extend(["--session-id", claude_session_id])
        cmd.append("--no-session-persistence")
    elif claude_session_id:
        cmd.extend(["--resume", claude_session_id])

    cmd.append(message)
    return cmd


def digest_claude_output(raw_output: str) -> dict:
    """Parse Claude's JSON output into a clean result dict.

    Filters system/hook_response and system/init blocks.
    Extracts: result, usage, cost_usd, duration_ms, session_id, num_turns, is_error.
    """
    result = {
        "result": "",
        "session_id": "",
        "cost_usd": 0.0,
        "duration_ms": 0,
        "num_turns": 0,
        "is_error": False,
        "usage": {},
        "tool_uses": [],
    }

    if not raw_output or not raw_output.strip():
        result["is_error"] = True
        result["result"] = "Empty output from Claude"
        return result

    # Try parsing as single JSON object (Claude --output-format json)
    try:
        data = json.loads(raw_output.strip())
    except json.JSONDecodeError:
        # Try last JSON line
        data = None
        for line in reversed(raw_output.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

    if data is None:
        result["result"] = raw_output.strip()
        return result

    # Extract fields from Claude JSON output
    result["session_id"] = data.get("session_id", data.get("sessionId", ""))
    result["cost_usd"] = data.get("cost_usd", 0.0)
    result["duration_ms"] = data.get("duration_ms", 0)
    result["num_turns"] = data.get("num_turns", 0)
    result["is_error"] = data.get("is_error", False)
    result["usage"] = data.get("usage", {})

    # Extract the actual result text
    raw_result = data.get("result", "")
    if isinstance(raw_result, str):
        result["result"] = raw_result
    elif isinstance(raw_result, list):
        # Filter out system blocks, extract text
        texts = []
        tool_uses = []
        for block in raw_result:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_uses.append({"name": block.get("name", ""), "input": block.get("input", {})})
        result["result"] = "\n".join(texts)
        result["tool_uses"] = tool_uses
    else:
        result["result"] = str(raw_result)

    return result


class AgentExecutor:
    """Executes agent requests by spawning Claude subprocess."""

    async def execute(
        self,
        message: str,
        external_uuid: str,
        wait: bool = True,
        stateless: bool = False,
        model: str = "",
        max_turns: int = 0,
        system_prompt: str = "",
        append_system_prompt: Optional[bool] = None,
        permission_mode: str = "",
        output_format: str = "",
        effort: str = "",
        max_budget_usd: float = 0,
        fallback_model: str = "",
        allowed_tools: str = "",
        disallowed_tools: str = "",
        timeout: int = 0,
        context: Optional[dict] = None,
        meta: Optional[dict] = None,
        disable_mcp_servers: Optional[list] = None,
    ) -> dict:
        """Execute an agent request.

        Returns a result dict with: result, session_id, cost_usd, duration_ms,
        num_turns, is_error, usage, tool_uses.
        """
        from agenticore.agent_mode.session_registry import (
            mark_session_complete,
            mark_session_failed,
            register_session,
        )
        from agenticore.agent_mode.state import save_state
        from agenticore.runner import build_subprocess_env

        cfg = get_config()
        am = cfg.agent_mode

        # Register session
        mapping = register_session(external_uuid, stateless=stateless)
        claude_session_id = mapping.claude_session_id

        # Save state for hooks
        save_state(external_uuid, wait=wait, meta=meta)

        # Build command
        cmd = build_claude_cmd(
            message,
            claude_session_id=claude_session_id,
            stateless=stateless,
            model=model,
            max_turns=max_turns,
            system_prompt=system_prompt,
            append_system_prompt=append_system_prompt,
            permission_mode=permission_mode,
            output_format=output_format,
            effort=effort,
            max_budget_usd=max_budget_usd,
            fallback_model=fallback_model,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

        # Build environment
        env = build_subprocess_env()
        env["AGENTICORE_CORRELATION_ID"] = external_uuid
        env["AGENTICORE_CLAUDE_SESSION_ID"] = claude_session_id

        # Set CWD to package dir
        cwd = Path(am.package_dir)
        if not cwd.exists():
            cwd = Path.cwd()

        # Pre-call: render MCP whitelist from .agentihooks.json
        try:
            from agenticore.hooks import render_mcp_whitelist
            render_mcp_whitelist(cwd, disable_servers=disable_mcp_servers)
        except Exception as e:
            _log.warning("Pre-call MCP render failed (non-fatal): %s", e)

        resolved_timeout = timeout or am.timeout
        start_time = time.monotonic()

        # Execute with retry
        last_error = None
        for attempt in range(am.max_retry_attempts):
            try:
                result = await self._run_subprocess(cmd, cwd, env, resolved_timeout, context)

                # After first call for persistent session: capture real Claude session ID
                if not stateless and not claude_session_id and result.get("session_id"):
                    claude_session_id = result["session_id"]
                    from agenticore.agent_mode.session_registry import update_session_claude_id

                    update_session_claude_id(external_uuid, claude_session_id)
                    env["AGENTICORE_CLAUDE_SESSION_ID"] = claude_session_id

                if result["is_error"] and attempt < am.max_retry_attempts - 1:
                    from agenticore.agent_mode.session_manager import detect_retryable_error, compose_retry_prompt

                    retryable = detect_retryable_error(result.get("result", ""), result.get("_stderr", ""))
                    if retryable:
                        _log.warning("Retryable error (attempt %d): %s", attempt + 1, retryable.message)
                        retry_msg = compose_retry_prompt(message, retryable)
                        cmd = build_claude_cmd(
                            retry_msg,
                            claude_session_id=claude_session_id,
                            stateless=False,  # resume for retry
                            model=model,
                            max_turns=max_turns,
                            system_prompt=system_prompt,
                            append_system_prompt=append_system_prompt,
                            permission_mode=permission_mode,
                            output_format=output_format,
                        )
                        await asyncio.sleep(min(2**attempt, 10))
                        continue

                result["duration_ms"] = int((time.monotonic() - start_time) * 1000)

                if result["is_error"]:
                    mark_session_failed(external_uuid)
                else:
                    mark_session_complete(external_uuid)

                result.pop("_stderr", None)
                return result

            except asyncio.TimeoutError:
                last_error = f"Timeout after {resolved_timeout}s"
                _log.error("Agent execution timeout (attempt %d)", attempt + 1)
            except Exception as e:
                last_error = str(e)
                _log.error("Agent execution error (attempt %d): %s", attempt + 1, e)

        # All retries exhausted
        mark_session_failed(external_uuid)
        return {
            "result": "",
            "session_id": claude_session_id,
            "cost_usd": 0.0,
            "duration_ms": int((time.monotonic() - start_time) * 1000),
            "num_turns": 0,
            "is_error": True,
            "error": last_error or "Unknown error",
            "usage": {},
            "tool_uses": [],
        }

    async def _run_subprocess(
        self, cmd: list, cwd: Path, env: dict, timeout: int, context: Optional[dict] = None
    ) -> dict:
        """Run the Claude subprocess and parse output."""
        stdin_data = None
        if context:
            stdin_data = json.dumps(context).encode()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate(input=stdin_data)

        output_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        error_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        result = digest_claude_output(output_text)
        result["_stderr"] = error_text

        if proc.returncode != 0 and not result["is_error"]:
            result["is_error"] = True
            if not result["result"]:
                result["result"] = error_text or f"Claude exited with code {proc.returncode}"

        return result

    async def _spawn_subprocess(
        self, cmd: list, cwd: Path, env: dict, context: Optional[dict] = None
    ) -> tuple[asyncio.subprocess.Process, Optional[bytes]]:
        """Spawn the Claude subprocess without awaiting communicate().

        Returns (process, stdin_data). Caller is responsible for draining
        stdout/stderr and awaiting completion.
        """
        stdin_data = json.dumps(context).encode() if context else None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc, stdin_data

    async def execute_streaming(
        self,
        message: str,
        external_uuid: str,
        stream_cfg: dict,
        model: str = "",
        max_turns: int = 0,
        system_prompt: str = "",
        append_system_prompt: Optional[bool] = None,
        permission_mode: str = "",
        effort: str = "",
        max_budget_usd: float = 0,
        fallback_model: str = "",
        allowed_tools: str = "",
        disallowed_tools: str = "",
        timeout: int = 0,
        context: Optional[dict] = None,
        meta: Optional[dict] = None,
        disable_mcp_servers: Optional[list] = None,
        request_uuid: str = "",
        sse_model_name: str = "",
    ):
        """Streaming variant of execute(). Yields SSE chunk strings.

        Spawns the Claude subprocess, then concurrently tails the per-uuid
        Redis Stream of hook events while the subprocess runs. Each visible
        event is mapped to an OpenAI delta chunk and yielded immediately.
        On done sentinel (or process exit), drains stdout, digests the
        result, and yields a final stop chunk + [DONE].
        """
        from agenticore.agent_mode.event_tailer import tail_event_stream
        from agenticore.agent_mode.openai_compat import (
            format_done,
            format_event_as_sse,
            format_role_open_chunk,
            format_stop_chunk,
        )
        from agenticore.agent_mode.session_registry import (
            mark_session_complete,
            mark_session_failed,
            register_session,
        )
        from agenticore.agent_mode.state import save_state
        from agenticore.runner import build_subprocess_env

        cfg = get_config()
        am = cfg.agent_mode

        # Always stateless for OpenAI-compat streaming (no multi-turn server-side memory)
        mapping = register_session(external_uuid, stateless=True)
        claude_session_id = mapping.claude_session_id
        save_state(external_uuid, wait=True, meta=meta)

        cmd = build_claude_cmd(
            message,
            claude_session_id=claude_session_id,
            stateless=True,
            model=model,
            max_turns=max_turns,
            system_prompt=system_prompt,
            append_system_prompt=append_system_prompt,
            permission_mode=permission_mode,
            effort=effort,
            max_budget_usd=max_budget_usd,
            fallback_model=fallback_model,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

        env = build_subprocess_env()
        env["AGENTICORE_CORRELATION_ID"] = external_uuid
        env["AGENTICORE_CLAUDE_SESSION_ID"] = claude_session_id
        env["AGENTICORE_EVENT_STREAM"] = "1"

        cwd = Path(am.package_dir)
        if not cwd.exists():
            cwd = Path.cwd()

        try:
            from agenticore.hooks import render_mcp_whitelist
            render_mcp_whitelist(cwd, disable_servers=disable_mcp_servers)
        except Exception as e:
            _log.warning("Pre-call MCP render failed (non-fatal): %s", e)

        resolved_timeout = timeout or am.timeout
        rid = request_uuid or external_uuid
        mdl = sse_model_name or model or am.model

        proc, stdin_data = await self._spawn_subprocess(cmd, cwd, env, context)

        if stdin_data and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass

        yield format_role_open_chunk(mdl, rid)

        try:
            async for event in tail_event_stream(external_uuid, stream_cfg, proc, resolved_timeout):
                if event.get("event_type") == "done":
                    break
                chunk = format_event_as_sse(event, mdl, rid)
                if chunk:
                    yield chunk
        except asyncio.CancelledError:
            try:
                proc.terminate()
            except Exception:
                pass
            raise

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=max(5, resolved_timeout)
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            stdout_bytes, stderr_bytes = b"", b""

        out_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        result = digest_claude_output(out_text)

        if proc.returncode and proc.returncode != 0 and not result["is_error"]:
            result["is_error"] = True

        if result["is_error"]:
            mark_session_failed(external_uuid)
        else:
            mark_session_complete(external_uuid)

        yield format_stop_chunk(result.get("usage", {}), mdl, rid)
        yield format_done()
