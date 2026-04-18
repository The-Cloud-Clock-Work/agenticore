"""Telegram connector — bridges Telegram bot to agenticore completions in-process.

Enable by setting env vars:
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_OWNER_ID   — numeric Telegram user ID (owner-only filter)

Optional:
  TELEGRAM_SYSTEM_PROMPT    — system prompt prepended to every conversation
  TELEGRAM_MAX_MESSAGES     — conversation history depth (default 20)
  TELEGRAM_CONVERSATION_TTL — history TTL in seconds (default 86400)
"""

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("agenticore.connectors.telegram")


def is_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


class ConversationStore:
    """In-memory conversation history per chat. Redis upgrade path available."""

    def __init__(self, max_messages: int = 20, ttl: int = 86400):
        self._store: dict[int, list[dict]] = {}
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


_chat_uuids: dict[int, str] = {}


def _uuid_for_chat(chat_id: int) -> str:
    """Stable UUID per Telegram chat — reused across messages for session continuity."""
    if chat_id not in _chat_uuids:
        import uuid
        _chat_uuids[chat_id] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"telegram:{chat_id}"))
    return _chat_uuids[chat_id]


async def _call_completions(messages: list[dict], chat_id: int) -> str:
    """Call agenticore's own completions handler in-process."""
    from agenticore.agent_mode.agent import AgentExecutor
    from agenticore.agent_mode.openai_compat import flatten_messages

    prompt = flatten_messages(messages)
    executor = AgentExecutor()

    result = await executor.execute(
        message=prompt,
        external_uuid=_uuid_for_chat(chat_id),
        wait=True,
    )

    if result.get("is_error"):
        raise RuntimeError(result.get("error", "Unknown agent error"))

    return result.get("result", "")


async def start(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start the Telegram bot polling loop. Runs forever until cancelled."""
    try:
        from aiogram import Bot, Dispatcher, F, Router
        from aiogram.filters import Command
        from aiogram.types import Message
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

    def _is_owner(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id == owner_id

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
        await message.answer(f"Messages: {count}\nAgent: {agent_id}")

    @router.message(F.text)
    async def handle_message(message: Message) -> None:
        if not _is_owner(message):
            return

        user_text = message.text
        if not user_text:
            return

        chat_id = message.chat.id
        messages = store.append(chat_id, "user", user_text)

        if system_prompt:
            api_messages = [{"role": "system", "content": system_prompt}] + messages
        else:
            api_messages = messages

        placeholder = await message.answer("...")

        try:
            response = await _call_completions(api_messages, chat_id=chat_id)
            store.append(chat_id, "assistant", response)

            if len(response) <= 4096:
                await placeholder.edit_text(response)
            else:
                await placeholder.edit_text(response[:4096])
                for i in range(4096, len(response), 4096):
                    await message.answer(response[i : i + 4096])

        except Exception as e:
            logger.exception("Completions call failed")
            await placeholder.edit_text(f"Error: {e}")

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram connector started (owner=%s)", owner_id)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
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
