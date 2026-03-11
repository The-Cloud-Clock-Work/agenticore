"""OpenAI-compatible chat completions adapter for agent mode.

Translates between OpenAI /v1/chat/completions format and Agenticore's
internal AgentExecutor interface. Pure functions — no I/O.
"""

import json
import time
from uuid import uuid4


def flatten_messages(messages: list[dict]) -> str:
    """Flatten OpenAI chat messages into a single prompt string.

    Single user message → content directly (no prefix).
    Multi-turn → role-prefixed lines joined by newlines.
    """
    if not messages:
        return ""

    if len(messages) == 1:
        return messages[0].get("content", "")

    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def build_openai_response(result: dict, model: str, request_uuid: str) -> dict:
    """Build an OpenAI-compatible chat completion response from agent result."""
    usage = result.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)

    return {
        "id": f"chatcmpl-{request_uuid}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.get("result", ""),
                },
                "finish_reason": "stop" if not result.get("is_error") else "error",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def build_openai_error(message: str, status: int) -> tuple[dict, int]:
    """Build an OpenAI-compatible error response."""
    return (
        {"error": {"message": message, "type": "server_error"}},
        status,
    )


def build_openai_stream_chunks(result: dict, model: str, request_uuid: str) -> str:
    """Build SSE-formatted streaming response from agent result.

    Returns the full SSE payload as a string (single content chunk + [DONE]).
    """
    usage = result.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    content = result.get("result", "")

    chunk = {
        "id": f"chatcmpl-{request_uuid}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    stop_chunk = {
        "id": f"chatcmpl-{request_uuid}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }

    return (
        f"data: {json.dumps(chunk)}\n\n"
        f"data: {json.dumps(stop_chunk)}\n\n"
        "data: [DONE]\n\n"
    )


def extract_request_id(headers: dict | None, body: dict) -> str:
    """Extract or generate a request UUID."""
    if headers:
        for key in ("x-request-id", "x_request_id"):
            if key in headers:
                return headers[key]
    return str(uuid4())
