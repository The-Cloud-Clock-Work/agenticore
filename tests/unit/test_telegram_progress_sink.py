"""Unit tests for TelegramProgressSink chip-based rendering."""

from __future__ import annotations

import pytest

from agenticore.connectors.telegram import (
    TelegramProgressSink,
    _extract_args_preview,
    _format_elapsed,
)


pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# Pure helpers


class TestExtractArgsPreview:
    def test_known_bash_uses_command(self):
        assert _extract_args_preview("Bash", {"command": "ls /tmp"}) == "ls /tmp"

    def test_known_websearch_uses_query(self):
        assert _extract_args_preview("WebSearch", {"query": "cantonese duck"}) == "cantonese duck"

    def test_mcp_tool_strips_prefix_and_matches_trailing(self):
        # Map "anton-docker" is not known, falls back to first scalar.
        out = _extract_args_preview("mcp__tools-infra__anton-docker", {"action": "ps"})
        assert out == "ps"

    def test_unknown_falls_back_to_first_scalar(self):
        out = _extract_args_preview("Unknown", {"foo": "", "bar": "hi", "baz": 3})
        assert out == "hi"

    def test_unknown_no_scalars_dumps_json(self):
        out = _extract_args_preview("X", {"obj": {"a": 1}})
        assert out.startswith("{") and "obj" in out

    def test_empty_args_returns_empty(self):
        assert _extract_args_preview("Bash", {}) == ""
        assert _extract_args_preview("Bash", None) == ""

    def test_truncates_long_value(self):
        long = "x" * 200
        out = _extract_args_preview("Bash", {"command": long}, max_len=80)
        assert len(out) == 80
        assert out.endswith("…")

    def test_strips_newlines(self):
        out = _extract_args_preview("Bash", {"command": "line1\nline2\rline3"})
        assert "\n" not in out and "\r" not in out


class TestFormatElapsed:
    def test_milliseconds(self):
        assert _format_elapsed(0.85) == "850ms"
        assert _format_elapsed(0.001) == "1ms"

    def test_seconds_one_decimal(self):
        assert _format_elapsed(1.2) == "1.2s"
        assert _format_elapsed(59.9) == "59.9s"

    def test_minutes(self):
        assert _format_elapsed(65) == "1m05s"
        assert _format_elapsed(125) == "2m05s"

    def test_negative(self):
        assert _format_elapsed(-1) == "?"


# ----------------------------------------------------------------------
# FakeBot double — records every API call, supports awaitable methods.


class _FakeMsg:
    def __init__(self, msg_id: int):
        self.message_id = msg_id


class FakeBot:
    def __init__(self):
        self.sends: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.deletes: list[int] = []
        self._next_id = 100
        self.delete_raises: Exception | None = None
        self.edit_raises: Exception | None = None

    async def send_message(self, chat_id, text):
        self.sends.append(text)
        msg = _FakeMsg(self._next_id)
        self._next_id += 1
        return msg

    async def edit_message_text(self, text, *, chat_id, message_id):
        if self.edit_raises is not None:
            raise self.edit_raises
        self.edits.append((message_id, text))

    async def delete_message(self, *, chat_id, message_id):
        if self.delete_raises is not None:
            raise self.delete_raises
        self.deletes.append(message_id)


# ----------------------------------------------------------------------
# Sink behaviour


class TestSinkChipLifecycle:
    @pytest.mark.asyncio
    async def test_tool_call_creates_progress_message_with_running_chip(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("Bash", {"command": "ls /tmp"}, "tu_1")
        assert len(bot.sends) == 1
        assert "▶ Bash: ls /tmp" in bot.sends[0]
        assert sink._progress_message_id == 100

    @pytest.mark.asyncio
    async def test_tool_result_transitions_chip_to_ok(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        # Force elapsed window to be >1s so the _flush isn't debounced.
        sink._last_edit_ts = 0.0
        await sink.on_tool_result("tu_1", "ok", is_error=False)
        # Expect one edit with "✓ Bash: ls (…)"
        assert len(bot.edits) == 1
        _, text = bot.edits[0]
        assert text.startswith("✓ Bash: ls (")
        assert text.endswith(")")

    @pytest.mark.asyncio
    async def test_tool_result_error_renders_x(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("Bash", {"command": "false"}, "tu_1")
        sink._last_edit_ts = 0.0
        await sink.on_tool_result("tu_1", "", is_error=True)
        _, text = bot.edits[0]
        assert text.startswith("✗ Bash: false (")

    @pytest.mark.asyncio
    async def test_multiple_tools_stack_as_chips(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        sink._last_edit_ts = 0.0
        await sink.on_tool_call("WebSearch", {"query": "python"}, "tu_2")
        assert any("▶ Bash: ls" in e[1] and "▶ WebSearch: python" in e[1] for e in bot.edits) or any(
            "▶ Bash: ls" in s and "▶ WebSearch: python" in s for s in bot.sends
        )

    @pytest.mark.asyncio
    async def test_unpaired_tool_result_synthesizes_chip(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        # No on_tool_call first — just a result
        await sink.on_tool_result("orphan", "output", is_error=False)
        assert len(sink._chips) == 1
        chip = next(iter(sink._chips.values()))
        assert chip.status == "ok"


class TestSinkFinalLifecycle:
    @pytest.mark.asyncio
    async def test_live_mode_deletes_progress_and_sends_final(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1, mode="live")
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        await sink.on_final("The answer is 42.")
        # progress message deleted
        assert 100 in bot.deletes
        # final sent as a new message
        assert "The answer is 42." in bot.sends[-1]

    @pytest.mark.asyncio
    async def test_transcript_mode_keeps_progress_and_sends_final(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1, mode="transcript")
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        sink._last_edit_ts = 0.0
        await sink.on_final("The answer is 42.")
        # progress NOT deleted
        assert bot.deletes == []
        # final sent as a new message
        assert "The answer is 42." in bot.sends[-1]

    @pytest.mark.asyncio
    async def test_on_final_is_idempotent(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1, mode="live")
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        await sink.on_final("ans")
        initial_sends = len(bot.sends)
        initial_deletes = len(bot.deletes)
        await sink.on_final("ans again")
        assert len(bot.sends) == initial_sends
        assert len(bot.deletes) == initial_deletes

    @pytest.mark.asyncio
    async def test_delete_failure_falls_back_to_marker_edit(self):
        bot = FakeBot()
        bot.delete_raises = RuntimeError("too old to delete")
        sink = TelegramProgressSink(bot, chat_id=1, mode="live")
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        await sink.on_final("ans")
        # Marker edit attempted
        assert any(e[1] == "✓" for e in bot.edits)
        # Final still sent
        assert "ans" in bot.sends[-1]

    @pytest.mark.asyncio
    async def test_on_final_without_progress_message_just_sends(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1, mode="live")
        await sink.on_final("no tools ran, just this")
        assert bot.deletes == []
        assert "no tools ran, just this" in bot.sends[-1]


class TestSinkNarration:
    @pytest.mark.asyncio
    async def test_narration_appears_below_chips(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("Bash", {"command": "ls"}, "tu_1")
        sink._last_edit_ts = 0.0
        await sink.on_narration("Looking at the output now.")
        # The rendered body should include both the chip and the narration
        rendered = sink._render()
        assert "▶ Bash: ls" in rendered
        assert "Looking at the output now." in rendered

    @pytest.mark.asyncio
    async def test_narration_only_creates_message(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_narration("thinking about it...")
        assert len(bot.sends) == 1
        assert "thinking about it..." in bot.sends[0]


class TestSinkChipArgsPreview:
    @pytest.mark.asyncio
    async def test_chip_includes_args_preview(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("WebSearch", {"query": "latest codex version"}, "tu_1")
        # Running chip includes the query
        assert "▶ WebSearch: latest codex version" in bot.sends[0]

    @pytest.mark.asyncio
    async def test_chip_no_args_skips_colon(self):
        bot = FakeBot()
        sink = TelegramProgressSink(bot, chat_id=1)
        await sink.on_tool_call("MysteryTool", {}, "tu_1")
        assert "▶ MysteryTool" in bot.sends[0]
        # No "MysteryTool: " with trailing colon
        assert "▶ MysteryTool:" not in bot.sends[0]
