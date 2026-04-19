"""Voice adapter implementations.

VoiceAdapter is the protocol any voice backend must satisfy.
HttpVoiceAdapter calls a remote HTTP service with /stt and /speak endpoints.
"""

import logging
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger("agenticore.voice")


class VoiceQuotaError(Exception):
    """Raised when the voice service reports quota exhaustion."""


@runtime_checkable
class VoiceAdapter(Protocol):

    async def transcribe(self, audio: bytes, mime_type: str = "audio/ogg") -> str:
        ...

    async def speak(self, text: str, output_format: str = "ogg") -> tuple[bytes, str]:
        ...


class HttpVoiceAdapter:
    """Calls a remote voice service over HTTP."""

    def __init__(self, base_url: str, timeout: float = 120.0, api_key: str | None = None):
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout, headers=headers)

    async def transcribe(self, audio: bytes, mime_type: str = "audio/ogg") -> str:
        ext = mime_type.split("/")[-1].replace("opus", "ogg")
        resp = await self._client.post(
            "/stt",
            files={"audio": (f"audio.{ext}", audio, mime_type)},
        )
        resp.raise_for_status()
        return resp.json()["text"]

    async def speak(self, text: str, output_format: str = "ogg") -> tuple[bytes, str]:
        resp = await self._client.post(
            "/speak",
            json={
                "text": text,
                "voice": {"output_format": output_format},
                "store": False,
            },
        )
        if resp.status_code == 429:
            body = resp.json()
            msg = body.get("message", "Voice service quota exceeded")
            raise VoiceQuotaError(msg)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", f"audio/{output_format}")
        return resp.content, content_type

    async def close(self) -> None:
        await self._client.aclose()
