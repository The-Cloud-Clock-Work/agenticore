---
title: Telegram Connector
nav_order: 9
parent: Reference
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
TELEGRAM_PROGRESS_MODE=live       # "live" deletes the transient progress
                                  # message when the final answer lands;
                                  # "transcript" keeps it for history
VOICE_SERVICE_URL=<url>           # enables voice message support
VOICE_DEFAULT_MODE=text           # default output mode: "text" or "voice"
```

Progress visibility (tool chips, narration, thinking) is controlled by
the per-agent sticky `stream_config` in Redis — send `/show-tools`,
`/hide-tools`, `/show-thinking`, etc. to the bot to toggle for the
current agent. See [SSE streaming](sse-streaming.md#slash-tokens-visibility-toggles)
for the full token list.

## Architecture

```
agenticore/connectors/telegram.py
    │
    ├── ConversationStore       — per-chat message history + voice mode state
    ├── TelegramProgressSink    — ProgressSink that renders tool-call chips +
    │                             narration into a transient progress message
    │                             while the agent runs, then deletes it and
    │                             sends the final answer as a new message
    ├── _SilentSink             — no-op sink used in voice mode (chat stays
    │                             clean; TTS handles the final answer)
    ├── _call_completions()     — routes to AgentExecutor in-process, passing
    │                             the sink so intermediate events flow
    ├── _parse_voice_commands() — regex intercept before LLM
    ├── _speak_or_fallback()    — voice or text output for repeat / voice-mode final
    │
    ├── F.text handler          — text messages
    ├── F.voice handler         — voice note transcription
    └── /start, /clear, /status, /voice commands
```

### What the user sees during a turn

1. User sends a prompt.
2. The bot creates a **transient progress message** with a chip per tool
   invocation:
   - `▶ Bash: docker ps --format ...` — tool is running
   - `✓ Bash: docker ps --format ... (1.2s)` — tool completed ok
   - `✗ Bash: false (50ms)` — tool errored
   Multiple tools stack as separate lines, each transitioning independently.
   Narration (if the model emits any) appears below the chips.
3. When the agent finishes, the progress message is **deleted** (live mode,
   default) and the **final answer is sent as a new persistent message**.
   In transcript mode, the progress message is kept instead of deleted.

Edits are debounced to one per second to respect Telegram's rate limit;
`on_tool_call` force-flushes so the "running" state appears even for
sub-second tools.

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
