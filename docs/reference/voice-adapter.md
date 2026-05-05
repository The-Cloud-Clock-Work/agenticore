---
title: Voice Adapter
nav_order: 8
parent: Reference
---

# Voice Adapter

Pluggable STT/TTS bridge that connects any agenticore instance to any HTTP
voice service. Decoupled from specific implementations — the adapter doesn't
know or care whether the backend is ElevenLabs, Deepgram, Whisper, or a
custom service.

## Enable

Set one environment variable:

```bash
VOICE_SERVICE_URL=http://anton-voice.anton-dev.svc:8400
```

Optional:

```bash
VOICE_API_KEY=your-api-key    # forwarded as Authorization header
```

## Architecture

```
agenticore/voice/__init__.py     — is_enabled(), get_adapter() singleton
agenticore/voice/adapter.py      — VoiceAdapter protocol + HttpVoiceAdapter
```

The adapter is stateless, instantiated on first use, and reused via a module-level
singleton. No ASGI lifespan changes needed.

## Protocol

```python
@runtime_checkable
class VoiceAdapter(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str = "audio/ogg") -> str: ...
    async def speak(self, text: str, output_format: str = "ogg") -> tuple[bytes, str]: ...
    async def close(self) -> None: ...
```

### `transcribe(audio, mime_type) → str`

Sends audio to the voice service for speech-to-text.

- **Method**: `POST /stt`
- **Content-Type**: `multipart/form-data`
- **Field**: `audio` (binary file)
- **Returns**: transcribed text string

### `speak(text, output_format) → (bytes, content_type)`

Sends text to the voice service for text-to-speech.

- **Method**: `POST /speak`
- **Body**: `{"text": "...", "voice": {"output_format": "ogg"}, "store": false}`
- **Returns**: tuple of `(audio_bytes, content_type_header)`

Setting `store: false` returns raw audio bytes instead of a stored file reference.

## HttpVoiceAdapter

The built-in implementation using `httpx.AsyncClient`:

```python
class HttpVoiceAdapter:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 120.0)
```

- `base_url` — voice service root (e.g. `http://anton-voice:8400`)
- `api_key` — optional, sent as `Authorization: Bearer <key>`
- `timeout` — request timeout in seconds (default 120, accommodates long TTS)

## Usage

```python
from agenticore.voice import is_enabled, get_adapter

if is_enabled():
    adapter = get_adapter()
    text = await adapter.transcribe(audio_bytes, "audio/ogg")
    audio, content_type = await adapter.speak("Hello world")
    await adapter.close()
```

## Error Handling

- `VoiceQuotaError` — raised when the voice service returns a quota/billing error.
  The Telegram connector surfaces this to the user instead of silently falling back.
- General HTTP errors raise `httpx.HTTPStatusError` — callers should handle gracefully.

## Compatible Services

Any HTTP service implementing:

- `POST /stt` — multipart upload, returns `{"text": "..."}`
- `POST /speak` — JSON body, returns raw audio bytes

The Anton voice service (`anton-voice`) implements this interface using
ElevenLabs/Deepgram/Cartesia backends.
