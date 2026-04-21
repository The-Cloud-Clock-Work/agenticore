"""Telegram connector — bridges Telegram bot to agenticore completions in-process.

Enable by setting env vars:
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_OWNER_ID   — numeric Telegram user ID (owner-only filter)

Optional:
  TELEGRAM_SYSTEM_PROMPT    — system prompt prepended to every conversation
  TELEGRAM_MAX_MESSAGES     — conversation history depth (default 20)
  TELEGRAM_CONVERSATION_TTL — history TTL in seconds (default 86400)
  TELEGRAM_PROGRESS_MODE    — "live" (default, edit one message with
                              narration → final) or "transcript"
                              (keep narration message, send final as new
                              message)

Voice support (requires agenticore.voice adapter):
  VOICE_SERVICE_URL   — base URL of voice HTTP service (enables voice)
  VOICE_DEFAULT_MODE  — default response mode: "text" (default) or "voice"
"""

import asyncio
import io
import logging
import os
import re
import time
from typing import Optional

from agenticore.connectors.base import ProgressSink

logger = logging.getLogger("agenticore.connectors.telegram")

# ---------------------------------------------------------------------------
# Voice command regex — intercepted before LLM, stripped from user input
# ---------------------------------------------------------------------------
_RE_VOICE_ON = re.compile(r"\b(enable\s+voice|voice\s+on|activate\s+voice)\b", re.IGNORECASE)
_RE_VOICE_OFF = re.compile(r"\b(disable\s+voice|voice\s+off|deactivate\s+voice)\b", re.IGNORECASE)
_RE_REPEAT = re.compile(
    r"\b(send\s+(me\s+)?(that|it|the\s+(last\s+)?message)\s+again"
    r"|repeat\s+(that|it|the\s+(last\s+)?message))\b",
    re.IGNORECASE,
)


def is_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


class ConversationStore:
    """In-memory conversation history per chat. Redis upgrade path available."""

    def __init__(self, max_messages: int = 20, ttl: int = 86400):
        self._store: dict[int, list[dict]] = {}
        self._voice_mode: dict[int, bool] = {}
        self._max = max_messages
        self._ttl = ttl

    def get(self, chat_id: int) -> list[dict]:
        return list(self._store.get(chat_id, []))

    def append(self, chat_id: int, role: str, content: str) -> list[dict]:
        history = self._store.setdefault(chat_id, [])
        history.append({"role": role, "content": content})
        if len(history) > self._max:
            self._store[chat_id] = history[-self._max :]
        return list(self._store[chat_id])

    def clear(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)

    def message_count(self, chat_id: int) -> int:
        return len(self._store.get(chat_id, []))

    def get_voice_mode(self, chat_id: int) -> bool:
        return self._voice_mode.get(chat_id, _voice_default())

    def set_voice_mode(self, chat_id: int, enabled: bool) -> None:
        self._voice_mode[chat_id] = enabled

    def get_last_assistant(self, chat_id: int) -> Optional[str]:
        history = self._store.get(chat_id, [])
        for msg in reversed(history):
            if msg["role"] == "assistant":
                return msg["content"]
        return None


def _voice_default() -> bool:
    return os.environ.get("VOICE_DEFAULT_MODE", "text").lower() == "voice"


_chat_uuids: dict[int, str] = {}


def _uuid_for_chat(chat_id: int) -> str:
    """Stable UUID per Telegram chat — reused across messages for session continuity."""
    if chat_id not in _chat_uuids:
        import uuid

        _chat_uuids[chat_id] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"telegram:{chat_id}"))
    return _chat_uuids[chat_id]


class TelegramProgressSink(ProgressSink):
    """ProgressSink that renders canonical agent events into Telegram messages.

    The rendering strategy is controlled by TELEGRAM_PROGRESS_MODE:

    - ``live`` (default) — single message is edited with narration +
      tool-call chips while the agent runs; when on_final fires the
      message is replaced with the authoritative final answer.
    - ``transcript`` — the progress message remains for history; the
      final answer is sent as a new message below it.

    Narration updates are debounced to ~1 edit per second to stay inside
    Telegram's rate limit, and each edit truncates to Telegram's 4096-
    character message limit from the tail (most recent content visible).
    """

    _MAX_TG_CHARS = 4000  # keep 96-char headroom under 4096 for safety
    _EDIT_INTERVAL_S = 1.0

    def __init__(self, bot, chat_id: int, *, show_tools: bool = False, mode: str = "live"):
        self._bot = bot
        self._chat_id = chat_id
        self._show_tools = show_tools
        self._mode = mode if mode in ("live", "transcript") else "live"
        self._buffer: str = ""
        self._live_message_id: Optional[int] = None
        self._last_edit_ts: float = 0.0
        self._edit_lock = asyncio.Lock()
        self._last_rendered: str = ""
        self._final_sent = False
        self.final_text: str = ""

    # ------------------------------------------------------------------
    # Internal helpers

    def _render_buffer(self) -> str:
        text = self._buffer
        if len(text) > self._MAX_TG_CHARS:
            text = "…" + text[-(self._MAX_TG_CHARS - 1) :]
        return text

    async def _flush(self, *, force: bool = False) -> None:
        if not self._buffer:
            return
        now = time.monotonic()
        if not force and now - self._last_edit_ts < self._EDIT_INTERVAL_S:
            return
        rendered = self._render_buffer()
        if rendered == self._last_rendered:
            return
        async with self._edit_lock:
            try:
                if self._live_message_id is None:
                    msg = await self._bot.send_message(self._chat_id, rendered)
                    self._live_message_id = msg.message_id
                else:
                    await self._bot.edit_message_text(
                        rendered,
                        chat_id=self._chat_id,
                        message_id=self._live_message_id,
                    )
                self._last_rendered = rendered
                self._last_edit_ts = time.monotonic()
            except Exception as e:
                logger.debug("telegram progress flush failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # ProgressSink overrides

    async def on_narration(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        await self._flush()

    async def on_tool_call(self, name: str, args: dict, tool_use_id: str) -> None:
        if not self._show_tools:
            return
        self._buffer += f"\n\n▶ {name}"
        await self._flush()

    async def on_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None:
        if not self._show_tools:
            return
        marker = "◉ error" if is_error else "◉ ok"
        self._buffer += f"\n{marker}"
        await self._flush()

    async def on_final(self, text: str) -> None:
        if self._final_sent:
            return
        self._final_sent = True
        self.final_text = text or ""

        # Flush any residual narration so the user sees the complete
        # progress before the final lands.
        await self._flush(force=True)

        try:
            if self._mode == "live" and self._live_message_id is not None:
                # Replace the live progress message with the final answer,
                # truncated from the head if it exceeds Telegram's limit.
                rendered = text or ""
                if len(rendered) > self._MAX_TG_CHARS:
                    rendered = rendered[: self._MAX_TG_CHARS - 1] + "…"
                async with self._edit_lock:
                    await self._bot.edit_message_text(
                        rendered,
                        chat_id=self._chat_id,
                        message_id=self._live_message_id,
                    )
            else:
                # transcript mode (or no live message was created) — send
                # as a new message, chunked at 4096 chars per Telegram.
                for i in range(0, len(text or ""), 4096):
                    await self._bot.send_message(self._chat_id, (text or "")[i : i + 4096])
        except Exception as e:
            # If edit fails (rate-limit, stale message), fall back to
            # sending a new message so the user always sees the final.
            logger.debug("telegram final edit failed, sending new message: %s", e)
            try:
                for i in range(0, len(text or ""), 4096):
                    await self._bot.send_message(self._chat_id, (text or "")[i : i + 4096])
            except Exception:
                logger.exception("telegram final send failed")

    async def on_error(self, message: str, code=None) -> None:
        try:
            await self._bot.send_message(self._chat_id, f"Error: {message}")
        except Exception:
            logger.exception("telegram error send failed")


class _SilentSink(ProgressSink):
    """Drops every event except final/error — used in voice mode where the
    user listens to the final answer and doesn't want narration spam in
    the chat."""

    def __init__(self):
        self.final_text: str = ""
        self.error_text: Optional[str] = None

    async def on_final(self, text: str) -> None:
        self.final_text = text or ""

    async def on_error(self, message: str, code=None) -> None:
        self.error_text = message


async def _call_completions(
    messages: list[dict],
    chat_id: int,
    *,
    sink: Optional[ProgressSink] = None,
) -> str:
    """Call agenticore's own completions handler in-process.

    When a sink is supplied, canonical progress events (narration, tool
    calls, final, usage, error) are dispatched to it as the agent works.
    Returns the final answer string either way so the caller can TTS it
    or log it.
    """
    from agenticore.agent_mode.agent import AgentExecutor
    from agenticore.agent_mode.openai_compat import flatten_messages

    prompt = flatten_messages(messages)
    executor = AgentExecutor()

    result = await executor.execute(
        message=prompt,
        external_uuid=_uuid_for_chat(chat_id),
        wait=True,
        sink=sink,
    )

    if result.get("is_error"):
        raise RuntimeError(result.get("error", "Unknown agent error"))

    return result.get("result", "")


def _parse_voice_commands(text: str, store: "ConversationStore", chat_id: int) -> tuple[str, bool]:
    """Parse and strip voice toggle commands. Returns (cleaned_text, mode_changed)."""
    changed = False

    if _RE_VOICE_ON.search(text):
        store.set_voice_mode(chat_id, True)
        text = _RE_VOICE_ON.sub("", text)
        changed = True

    if _RE_VOICE_OFF.search(text):
        store.set_voice_mode(chat_id, False)
        text = _RE_VOICE_OFF.sub("", text)
        changed = True

    # clean up leftover punctuation/whitespace from stripping
    text = re.sub(r"^[\s,.:;]+|[\s,.:;]+$", "", text)
    return text, changed


def _is_repeat_command(text: str) -> bool:
    return bool(_RE_REPEAT.search(text))


def _get_voice_adapter():
    """Get the voice adapter if enabled, or None."""
    from agenticore.voice import is_enabled as voice_enabled, get_adapter

    if voice_enabled():
        return get_adapter()
    return None


async def start(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start the Telegram bot polling loop. Runs forever until cancelled."""
    try:
        from aiogram import Bot, Dispatcher, F, Router
        from aiogram.filters import Command, CommandObject
        from aiogram.types import BufferedInputFile, Message
    except ImportError:
        logger.error("aiogram not installed — pip install aiogram>=3.15")
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    owner_id = int(os.environ["TELEGRAM_OWNER_ID"])
    system_prompt = os.environ.get("TELEGRAM_SYSTEM_PROMPT", "")
    max_messages = int(os.environ.get("TELEGRAM_MAX_MESSAGES", "20"))
    ttl = int(os.environ.get("TELEGRAM_CONVERSATION_TTL", "86400"))

    store = ConversationStore(max_messages=max_messages, ttl=ttl)
    router = Router()
    voice_adapter = _get_voice_adapter()

    if voice_adapter:
        logger.info("Voice adapter enabled: %s", os.environ["VOICE_SERVICE_URL"])

    def _is_owner(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == owner_id

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    async def _send_text(message: Message, text: str) -> None:
        for i in range(0, len(text), 4096):
            await message.answer(text[i : i + 4096])

    async def _speak_or_fallback(message: Message, text: str, chat_id: int) -> None:
        """Send a single text response as voice (if voice mode + adapter), with
        text fallback on TTS failure. Used from the repeat-command path and
        from the voice-mode final dispatch in _process_and_respond."""
        if store.get_voice_mode(chat_id) and voice_adapter:
            try:
                await bot.send_chat_action(chat_id, "record_voice")
                audio_bytes, _ = await voice_adapter.speak(text)
                voice_file = BufferedInputFile(audio_bytes, filename="response.ogg")
                await bot.send_voice(chat_id, voice_file)
            except Exception as exc:
                from agenticore.voice.adapter import VoiceQuotaError

                if isinstance(exc, VoiceQuotaError):
                    logger.error("Voice quota exceeded: %s", exc)
                    await message.answer(f"[Voice unavailable] {exc}\n\n{text}")
                else:
                    logger.warning("TTS failed, falling back to text", exc_info=True)
                    await _send_text(message, text)
        else:
            await _send_text(message, text)

    # ------------------------------------------------------------------
    # Core processing — shared by text and voice handlers
    # ------------------------------------------------------------------

    progress_mode = os.environ.get("TELEGRAM_PROGRESS_MODE", "live")
    show_tools_in_progress = os.environ.get("TELEGRAM_SHOW_TOOLS", "false").lower() in ("1", "true", "yes")

    async def _process_and_respond(message: Message, user_text: str, chat_id: int) -> None:
        # 1. Parse voice commands
        user_text, mode_changed = _parse_voice_commands(user_text, store, chat_id)

        # 2. Check for repeat command
        if _is_repeat_command(user_text):
            last = store.get_last_assistant(chat_id)
            if last:
                await _speak_or_fallback(message, last, chat_id)
            else:
                await message.answer("No previous message to repeat.")
            return

        # 3. If only a voice command with no remaining content, acknowledge
        if not user_text.strip():
            if mode_changed:
                mode = "voice" if store.get_voice_mode(chat_id) else "text"
                await message.answer(f"Voice mode: {mode}")
            return

        # 4. Normal flow — LLM completion with live progress sink
        messages = store.append(chat_id, "user", user_text)
        from agenticore.capabilities import render_capabilities_prompt

        caps = render_capabilities_prompt()
        full_system = "\n\n".join(filter(None, [system_prompt, caps]))
        if full_system:
            api_messages = [{"role": "system", "content": full_system}] + messages
        else:
            api_messages = messages

        await bot.send_chat_action(chat_id, "typing")

        voice_mode = store.get_voice_mode(chat_id) and voice_adapter is not None
        sink: ProgressSink
        if voice_mode:
            # Voice mode: suppress chat narration — user wants the voice
            # reply to be the authoritative channel. Sink collects the
            # final text and any error for us to TTS after execute returns.
            sink = _SilentSink()
        else:
            sink = TelegramProgressSink(
                bot,
                chat_id,
                show_tools=show_tools_in_progress,
                mode=progress_mode,
            )

        try:
            response = await _call_completions(api_messages, chat_id=chat_id, sink=sink)
            store.append(chat_id, "assistant", response)
            if voice_mode:
                # Sink did not render anything in chat — do the voice TTS now.
                await _speak_or_fallback(message, response, chat_id)
        except Exception as e:
            logger.exception("Completions call failed")
            await message.answer(f"Error: {e}")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not _is_owner(message):
            return
        await message.answer("Online. Connected to agenticore.")

    @router.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if not _is_owner(message):
            return
        store.clear(message.chat.id)
        await message.answer("Conversation cleared.")

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not _is_owner(message):
            return
        count = store.message_count(message.chat.id)
        agent_id = os.environ.get("AGENTIHUB_AGENT", "default")
        voice = "on" if store.get_voice_mode(message.chat.id) else "off"
        voice_svc = "connected" if voice_adapter else "not configured"
        await message.answer(f"Messages: {count}\nAgent: {agent_id}\nVoice mode: {voice}\nVoice service: {voice_svc}")

    @router.message(Command("voice"))
    async def cmd_voice(message: Message, command: CommandObject) -> None:
        if not _is_owner(message):
            return
        arg = (command.args or "").strip().lower()
        chat_id = message.chat.id
        if arg == "on":
            store.set_voice_mode(chat_id, True)
            await message.answer("Voice mode: on")
        elif arg == "off":
            store.set_voice_mode(chat_id, False)
            await message.answer("Voice mode: off")
        else:
            mode = "on" if store.get_voice_mode(chat_id) else "off"
            await message.answer(f"Voice mode: {mode}")

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    @router.message(F.voice)
    async def handle_voice(message: Message) -> None:
        if not _is_owner(message):
            return

        if not voice_adapter:
            await message.answer("Voice not configured. Set VOICE_SERVICE_URL.")
            return

        chat_id = message.chat.id
        await bot.send_chat_action(chat_id, "typing")

        try:
            file = await bot.get_file(message.voice.file_id)
            bio: io.BytesIO = await bot.download_file(file.file_path)
            audio_bytes = bio.read()
            user_text = await voice_adapter.transcribe(audio_bytes, "audio/ogg")
        except Exception:
            logger.exception("STT failed")
            await message.answer("Couldn't transcribe voice message. Try again.")
            return

        if not user_text.strip():
            await message.answer("Couldn't understand the voice message.")
            return

        await _process_and_respond(message, user_text, chat_id)

    @router.message(F.text)
    async def handle_message(message: Message) -> None:
        if not _is_owner(message):
            return

        user_text = message.text
        if not user_text:
            return

        await _process_and_respond(message, user_text, message.chat.id)

    # ------------------------------------------------------------------
    # Bot startup
    # ------------------------------------------------------------------

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram connector started (owner=%s, voice=%s)", owner_id, bool(voice_adapter))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        if voice_adapter:
            await voice_adapter.close()
        await bot.session.close()


async def start_with_reconnect() -> None:
    """Start with automatic reconnect on crash. Use as an asyncio task."""
    while True:
        try:
            await start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram connector crashed — restarting in 5s")
            await asyncio.sleep(5)
