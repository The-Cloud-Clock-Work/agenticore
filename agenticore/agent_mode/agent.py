"""Core agent execution engine.

Builds Claude CLI commands from request parameters and manages execution.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from agenticore.config import get_config
from agenticore.profiles import ProfileClaude, get_profile

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


# ── Active profile resolution (agentihooks profile owns CLI behavior) ─────


_cached_profile_claude: Optional[ProfileClaude] = None
_profile_claude_loaded = False


def _active_profile_claude() -> ProfileClaude:
    """Return the ProfileClaude config from the active agentihooks profile.

    Resolution: AGENTIHOOKS_PROFILE → ``get_profile()``. If unset or the
    profile cannot be loaded, returns a default ``ProfileClaude()`` instance
    so callers always get a valid object.

    Cached for the lifetime of the process.
    """
    global _cached_profile_claude, _profile_claude_loaded
    if _profile_claude_loaded:
        return _cached_profile_claude  # type: ignore[return-value]
    name = os.getenv("AGENTIHOOKS_PROFILE", "").strip()
    pc: Optional[ProfileClaude] = None
    if name:
        prof = get_profile(name)
        if prof is not None:
            pc = prof.claude
    if pc is None:
        pc = ProfileClaude()
    _cached_profile_claude = pc
    _profile_claude_loaded = True
    return pc


def reset_profile_claude_cache() -> None:
    """Reset the cached ProfileClaude (for testing)."""
    global _cached_profile_claude, _profile_claude_loaded
    _cached_profile_claude = None
    _profile_claude_loaded = False


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
    resume: bool = False,
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
    pc = _active_profile_claude()

    cmd = ["claude", "-p"]
    resolved_fmt = output_format or pc.output_format
    cmd.extend(["--output-format", resolved_fmt])
    if resolved_fmt == "stream-json":
        cmd.append("--verbose")
        cmd.append("--include-partial-messages")
    cmd.extend(["--model", model or pc.model])
    cmd.extend(["--max-turns", str(max_turns or pc.max_turns)])
    cmd.extend(["--permission-mode", permission_mode or pc.permission_mode])

    # System prompt resolution
    use_append = append_system_prompt if append_system_prompt is not None else am.append_system_prompt

    from agenticore.capabilities import render_capabilities_prompt

    caps_prompt = render_capabilities_prompt()

    if system_prompt:
        # Explicit inline override — always replaces
        cmd.extend(["--system-prompt", system_prompt])
        if caps_prompt:
            cmd.extend(["--append-system-prompt", caps_prompt])
    else:
        # Merge package system.md + capabilities into a single prompt payload.
        # Claude CLI >=2.1 rejects passing both --append-system-prompt and
        # --append-system-prompt-file in the same invocation, so we collapse
        # them into one string before emitting the flag.
        system_md_path = Path(am.package_dir) / "system.md"
        file_prompt = ""
        if system_md_path.exists():
            try:
                file_prompt = system_md_path.read_text().strip()
            except Exception as e:
                _log.warning("Failed to read system.md at %s: %s", system_md_path, e)

        if use_append:
            merged = "\n\n".join(p for p in (file_prompt, caps_prompt) if p)
            if merged:
                cmd.extend(["--append-system-prompt", merged])
        else:
            if file_prompt:
                cmd.extend(["--system-prompt-file", str(system_md_path)])
            if caps_prompt:
                cmd.extend(["--append-system-prompt", caps_prompt])

    # Optional flags
    resolved_effort = effort or (pc.effort or "")
    if resolved_effort:
        cmd.extend(["--effort", resolved_effort])
    if max_budget_usd > 0:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if fallback_model:
        cmd.extend(["--fallback-model", fallback_model])
    if allowed_tools:
        parsed = [t.strip() for t in allowed_tools.split(",") if t.strip()]
        if parsed:
            cmd.extend(["--allowedTools"] + parsed)
    if disallowed_tools:
        parsed = [t.strip() for t in disallowed_tools.split(",") if t.strip()]
        if parsed:
            cmd.extend(["--disallowedTools"] + parsed)

    # Session handling: stateless → one-shot; resume → continue existing; else → create persistent
    if stateless:
        cmd.extend(["--session-id", claude_session_id])
        cmd.append("--no-session-persistence")
    elif resume and claude_session_id:
        cmd.extend(["--resume", claude_session_id])
    elif claude_session_id:
        cmd.extend(["--session-id", claude_session_id])

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
        sink: Optional["object"] = None,
        stream_cfg: Optional[dict] = None,
    ) -> dict:
        """Execute an agent request.

        Returns a result dict with: result, session_id, cost_usd, duration_ms,
        num_turns, is_error, usage, tool_uses.

        When ``sink`` is a ProgressSink, canonical progress events are
        dispatched to it as the subprocess runs — narration between tool
        calls, tool_call / tool_result, thinking, and the final answer.
        The sink's visibility is gated by ``stream_cfg``; if not supplied,
        the agent-scoped sticky config is loaded from Redis. Connectors
        (Telegram, Slack, Teams, …) use this to surface intermediate
        progress identically to how SSE clients see it.
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

        # Resolve stream_cfg for sink visibility (agent-scoped sticky config)
        if sink is not None and stream_cfg is None:
            try:
                from agenticore.agent_mode.stream_config import load_stream_config

                agent_id = os.environ.get("AGENTIHUB_AGENT", "default")
                stream_cfg = load_stream_config(agent_id)
            except Exception as e:
                _log.warning("Could not load stream_cfg for sink (non-fatal): %s", e)
                stream_cfg = {}

        # Build command
        cmd = build_claude_cmd(
            message,
            claude_session_id=claude_session_id,
            stateless=stateless,
            resume=not stateless and bool(claude_session_id),
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
        # The event_relay hook self-gates on AGENTICORE_EVENT_STREAM=1. Enable
        # it when a sink is attached so PostToolUse / Stop hooks publish
        # intermediate events to agenticore:events:{correlation_id} for the
        # Redis tailer to forward to the ProgressSink.
        if sink is not None:
            env["AGENTICORE_EVENT_STREAM"] = "1"

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
                result = await self._run_subprocess(
                    cmd,
                    cwd,
                    env,
                    resolved_timeout,
                    context,
                    sink=sink,
                    correlation_id=external_uuid,
                    stream_cfg=stream_cfg,
                )

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
                            stateless=False,
                            resume=bool(claude_session_id),
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
                elif stateless:
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
        self,
        cmd: list,
        cwd: Path,
        env: dict,
        timeout: int,
        context: Optional[dict] = None,
        sink: Optional[object] = None,
        correlation_id: str = "",
        stream_cfg: Optional[dict] = None,
    ) -> dict:
        """Run the Claude subprocess and parse output.

        If ``sink`` is provided, spawns a Redis event tailer in parallel
        with the subprocess and dispatches canonical ProgressEvents to the
        sink as they land. The subprocess itself still runs with the
        caller's configured output_format (typically aggregated JSON) —
        the Redis bus is the intermediate-event source.
        """
        stdin_data = None
        if context:
            stdin_data = json.dumps(context).encode()

        # Snapshot the Redis stream head BEFORE the subprocess runs so the
        # sink tailer only consumes events this turn publishes — otherwise
        # chat-scoped correlation ids replay prior turns' events on every call.
        stream_start_id = "0-0"
        if sink is not None and correlation_id:
            try:
                from agenticore.agent_mode.event_tailer import snapshot_stream_head

                stream_start_id = snapshot_stream_head(correlation_id)
            except Exception as exc:
                _log.warning("stream head snapshot failed (non-fatal): %s", exc)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        sink_task: Optional[asyncio.Task] = None
        guarded_sink = None
        if sink is not None and correlation_id:
            from agenticore.agent_mode.progress import (
                OnceSink,
                dispatch_to_sink,
                tail_canonical_from_redis,
            )

            cfg_for_tail = stream_cfg or {}
            guarded_sink = OnceSink(sink)

            async def _drive_sink() -> None:
                try:
                    async for evt in tail_canonical_from_redis(
                        correlation_id,
                        cfg_for_tail,
                        proc,
                        timeout,
                        start_id=stream_start_id,
                    ):
                        await dispatch_to_sink(evt, guarded_sink)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.warning("sink drive loop failed (non-fatal): %s", exc)

            sink_task = asyncio.create_task(_drive_sink())

        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await proc.communicate(input=stdin_data)
        finally:
            if sink_task is not None:
                # Give the Redis tailer a short grace window to drain the
                # ``done`` sentinel the hook emits on Stop, then cancel.
                try:
                    await asyncio.wait_for(sink_task, timeout=5)
                except asyncio.TimeoutError:
                    sink_task.cancel()
                    try:
                        await sink_task
                    except Exception:
                        pass
                except Exception:
                    pass

        output_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        error_text = stderr.decode("utf-8", errors="replace") if stderr else ""

        result = digest_claude_output(output_text)
        result["_stderr"] = error_text

        if proc.returncode != 0 and not result["is_error"]:
            result["is_error"] = True
            if not result["result"]:
                result["result"] = error_text or f"Claude exited with code {proc.returncode}"

        # Surface a concrete error message so HTTP callers (and LibreChat) see
        # the real failure instead of a generic "Agent error". digest_claude_output
        # writes subprocess failures into result["result"]; the actual stderr
        # from the CLI is in error_text. Prefer stderr when it has content,
        # otherwise fall back to the result text.
        if result["is_error"] and not result.get("error"):
            stderr_trim = (error_text or "").strip()
            result_trim = (result.get("result") or "").strip()
            result["error"] = stderr_trim or result_trim or f"Claude exited with code {proc.returncode}"

        # Terminal-signal fallback: connectors must always see an on_final
        # (or on_error) per turn. OnceSink de-dupes if the Redis tailer
        # already delivered these via the done sentinel.
        if guarded_sink is not None:
            try:
                if result["is_error"]:
                    await guarded_sink.on_error(result.get("result") or "agent error", None)
                elif result.get("result"):
                    await guarded_sink.on_final(result["result"])
                if result.get("usage"):
                    await guarded_sink.on_usage(result["usage"])
            except Exception as exc:
                _log.warning("sink terminal dispatch failed (non-fatal): %s", exc)

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
        claude_session_id: str = "",
        stateless: bool = True,
        resume: bool = False,
    ):
        """Streaming variant of execute(). Yields SSE chunk strings.

        Reads claude's stdout as line-delimited stream-json events
        (produced by --output-format stream-json --verbose
        --include-partial-messages) and dispatches them directly to
        SSE formatters, so thinking / text / tool calls arrive
        progressively as the model emits them. No Redis bus.
        """
        from agenticore.agent_mode.openai_compat import (
            format_done,
            format_final_delta,
            format_narration_delta,
            format_role_open_chunk,
            format_stop_chunk,
            format_thinking_delta,
            format_tool_result_delta,
            format_tool_use_delta,
        )
        from agenticore.agent_mode.progress import (
            CanonicalTranslator,
            ProgressEventType,
            progress_is_visible,
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

        register_session(external_uuid, stateless=stateless)
        save_state(external_uuid, wait=True, meta=meta)

        cmd = build_claude_cmd(
            message,
            claude_session_id=claude_session_id,
            stateless=stateless,
            resume=resume,
            model=model,
            max_turns=max_turns,
            system_prompt=system_prompt,
            append_system_prompt=append_system_prompt,
            permission_mode=permission_mode,
            output_format="stream-json",
            effort=effort,
            max_budget_usd=max_budget_usd,
            fallback_model=fallback_model,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

        env = build_subprocess_env()
        env["AGENTICORE_CORRELATION_ID"] = external_uuid
        env["AGENTICORE_CLAUDE_SESSION_ID"] = claude_session_id

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
        mdl = sse_model_name or model or _active_profile_claude().model

        proc, stdin_data = await self._spawn_subprocess(cmd, cwd, env, context)

        if stdin_data and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass

        yield format_role_open_chunk(mdl, rid)

        translator = CanonicalTranslator(correlation_id=rid, session_id=claude_session_id)
        usage: dict = {}
        final_is_error = False

        async def _readline() -> Optional[bytes]:
            try:
                return await asyncio.wait_for(proc.stdout.readline(), timeout=resolved_timeout)
            except asyncio.TimeoutError:
                return None

        def _format_canonical(canonical) -> Optional[str]:
            """Translate one canonical ProgressEvent into one SSE chunk."""
            ctype = canonical.type
            pl = canonical.payload or {}
            if ctype is ProgressEventType.NARRATION:
                return format_narration_delta(pl.get("text", ""), mdl, rid)
            if ctype is ProgressEventType.FINAL:
                return format_final_delta(pl.get("text", ""), mdl, rid)
            if ctype is ProgressEventType.THINKING:
                return format_thinking_delta(pl.get("text", ""), mdl, rid)
            if ctype is ProgressEventType.TOOL_CALL:
                return format_tool_use_delta(
                    json.dumps(
                        {
                            "id": pl.get("tool_use_id", ""),
                            "name": pl.get("name", ""),
                            "input": pl.get("args", {}) or {},
                        }
                    ),
                    mdl,
                    rid,
                )
            if ctype is ProgressEventType.TOOL_RESULT:
                return format_tool_result_delta(
                    json.dumps(
                        {
                            "tool_use_id": pl.get("tool_use_id", ""),
                            "is_error": bool(pl.get("is_error", False)),
                            "output": pl.get("content", ""),
                        }
                    ),
                    mdl,
                    rid,
                )
            return None

        try:
            while True:
                line = await _readline()
                if line is None:
                    break
                if not line:
                    if proc.returncode is not None:
                        break
                    continue
                try:
                    evt = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue

                for canonical in translator.feed_stdout(evt):
                    if canonical.type is ProgressEventType.USAGE:
                        usage = canonical.payload
                        final_is_error = bool(canonical.payload.get("is_error", False))
                        continue
                    if not progress_is_visible(canonical, stream_cfg):
                        continue
                    chunk = _format_canonical(canonical)
                    if chunk:
                        yield chunk

            # Flush any residual buffered text as FINAL if the turn ended
            # without a result event (unusual, but guard against it).
            for canonical in translator.flush():
                if not progress_is_visible(canonical, stream_cfg):
                    continue
                chunk = _format_canonical(canonical)
                if chunk:
                    yield chunk
        except asyncio.CancelledError:
            try:
                proc.terminate()
            except Exception:
                pass
            raise

        try:
            await asyncio.wait_for(proc.wait(), timeout=max(5, resolved_timeout))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass

        if proc.returncode and proc.returncode != 0:
            final_is_error = True

        if final_is_error:
            mark_session_failed(external_uuid)
        elif stateless:
            mark_session_complete(external_uuid)

        stop_usage = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }
        yield format_stop_chunk(stop_usage, mdl, rid)
        yield format_done()
