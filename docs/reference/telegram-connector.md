---
title: Telegram Connector
nav_order: 9
---

# Telegram Connector

Native Telegram bot connector that bridges Telegram messages to agenticore's
completions engine in-process. Replaces the old Claude Code plugin approach.

## Enable

Set environment variables:

```bash
TELEGRAM_BOT_TOKEN=<token from @BotFather>
TELEGRAM_OWNER_ID=<numeric Telegram user ID>
```

Optional:

```bash
TELEGRAM_SYSTEM_PROMPT=<custom system prompt>
TELEGRAM_MAX_MESSAGES=20          # conversation history depth
TELEGRAM_CONVERSATION_TTL=86400   # history TTL in seconds
VOICE_SERVICE_URL=<url>           # enables voice message support
VOICE_DEFAULT_MODE=text           # default output mode: "text" or "voice"
```

## Architecture

```
agenticore/connectors/telegram.py
    │
    ├── ConversationStore     — per-chat message history + voice mode state
    ├── _call_completions()   — routes to AgentExecutor in-process
    ├── _parse_voice_commands() — regex intercept before LLM
    ├── _send_in_mode()       — voice or text output based on mode
    │
    ├── F.text handler        — text messages
    ├── F.voice handler       — voice note transcription
    └── /start, /clear, /status, /voice commands
```

The connector uses [aiogram](https://docs.aiogram.dev/) v3 for Telegram Bot API
interaction. It runs as an async polling loop inside the agenticore process.

## Features

### Text Messages

User sends text → stored in conversation history → sent to AgentExecutor →
response sent back as text (or voice if voice mode is on).

### Voice Messages

User sends voice note (OGG/Opus) → downloaded via Bot API → transcribed via
[Voice Adapter](voice-adapter.md) → processed same as text → response in
current output mode.

Voice input is **always transcribed** regardless of voice mode setting.
Mode controls output format only.

### Voice Mode Toggle

Per-conversation toggle controlling whether responses are sent as text or
voice notes.

**Regex commands** (intercepted before LLM, stripped from input):

| Pattern | Action |
|---------|--------|
| `enable voice` / `voice on` / `activate voice` | Voice mode ON |
| `disable voice` / `voice off` / `deactivate voice` | Voice mode OFF |
| `send me that again` / `repeat that` | Re-send last response in current mode |

**Slash command**: `/voice [on|off]`

Combined commands work: "enable voice, send me that again" → toggles on →
re-sends last response as voice.

### Owner-Only Filter

All handlers check `message.from_user.id == TELEGRAM_OWNER_ID`. Messages from
other users are silently ignored.

### Auto-Reconnect

`start_with_reconnect()` wraps the polling loop with automatic restart on
crash (5s backoff).

## Conversation Store

In-memory per-chat history with configurable depth and TTL:

```python
class ConversationStore:
    get(chat_id) → list[dict]            # message history
    append(chat_id, role, content)        # add message
    clear(chat_id)                        # reset history
    get_voice_mode(chat_id) → bool        # current output mode
    set_voice_mode(chat_id, enabled)      # toggle output mode
    get_last_assistant(chat_id) → str     # for repeat command
```

## Capabilities Injection

The [Self-Describing Capabilities](../architecture/capabilities.md) module
automatically appends a capabilities block to the system prompt, so the agent
knows it has Telegram and voice features without explicit configuration.

## Chat Actions

The connector sends appropriate chat actions for user feedback:

- `typing` — while waiting for LLM response (text mode)
- `record_voice` — while generating TTS response (voice mode)

## Graceful Degradation

- **No voice service** → voice notes get "Voice not configured" reply
- **TTS failure** → falls back to text with log warning
- **Voice quota exceeded** → surfaces error to user with text fallback
- **STT failure** → "Couldn't transcribe, try again" reply
