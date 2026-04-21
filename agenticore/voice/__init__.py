"""Voice adapter — pluggable STT/TTS bridge for any HTTP voice service.

Enable by setting VOICE_SERVICE_URL to the base URL of a service
exposing POST /stt (audio→text) and POST /speak (text→audio).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenticore.voice.adapter import HttpVoiceAdapter

_adapter: HttpVoiceAdapter | None = None


def is_enabled() -> bool:
    return bool(os.environ.get("VOICE_SERVICE_URL"))


def get_adapter() -> HttpVoiceAdapter:
    global _adapter
    if _adapter is None:
        from agenticore.voice.adapter import HttpVoiceAdapter as _Cls

        url = os.environ["VOICE_SERVICE_URL"]
        api_key = os.environ.get("VOICE_API_KEY")
        _adapter = _Cls(url, api_key=api_key)
    return _adapter
