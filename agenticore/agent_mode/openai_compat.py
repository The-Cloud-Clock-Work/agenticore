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


def _chunk_envelope(model: str, request_uuid: str) -> dict:
    return {
        "id": f"chatcmpl-{request_uuid}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def format_role_open_chunk(model: str, request_uuid: str) -> str:
    """First SSE chunk: opens the assistant role."""
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    return _sse(env)


def format_text_delta(text: str, model: str, request_uuid: str) -> str:
    """Streaming content delta for assistant_text events."""
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    return _sse(env)


def format_thinking_delta(text: str, model: str, request_uuid: str) -> str:
    """Streaming delta for thinking blocks. Marked with x_agenticore_event_type."""
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [
        {
            "index": 0,
            "delta": {"content": text},
            "finish_reason": None,
            "x_agenticore_event_type": "thinking",
        }
    ]
    return _sse(env)


def format_tool_use_delta(tool_use_json: str, model: str, request_uuid: str) -> str:
    """Streaming delta for tool_use blocks. Maps to OpenAI tool_calls schema."""
    try:
        data = json.loads(tool_use_json) if isinstance(tool_use_json, str) else tool_use_json
    except Exception:
        data = {}
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [
        {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": data.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": data.get("name", ""),
                            "arguments": json.dumps(data.get("input", {})),
                        },
                    }
                ],
            },
            "finish_reason": None,
            "x_agenticore_event_type": "tool_use",
        }
    ]
    return _sse(env)


def format_tool_result_delta(tool_result_json: str, model: str, request_uuid: str) -> str:
    """Streaming delta for tool_result blocks. Custom event_type marker."""
    try:
        data = json.loads(tool_result_json) if isinstance(tool_result_json, str) else tool_result_json
    except Exception:
        data = {"output": str(tool_result_json)}
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [
        {
            "index": 0,
            "delta": {"content": data.get("output", "")},
            "finish_reason": None,
            "x_agenticore_event_type": "tool_result",
            "x_agenticore_tool_use_id": data.get("tool_use_id", ""),
            "x_agenticore_is_error": bool(data.get("is_error", False)),
        }
    ]
    return _sse(env)


def format_stop_chunk(usage: dict, model: str, request_uuid: str) -> str:
    """Final SSE chunk: finish_reason=stop with usage stats."""
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    env["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    return _sse(env)


def format_done() -> str:
    return "data: [DONE]\n\n"


def format_status_meta(stream_cfg: dict, model: str, request_uuid: str) -> str:
    """Meta SSE chunk for /stream-status — reports current visibility config."""
    env = _chunk_envelope(model, request_uuid)
    env["choices"] = [
        {
            "index": 0,
            "delta": {"content": json.dumps(stream_cfg)},
            "finish_reason": None,
            "x_agenticore_event_type": "stream_config",
        }
    ]
    return _sse(env)


def format_event_as_sse(event: dict, model: str, request_uuid: str) -> str | None:
    """Dispatch a raw stream event dict to its formatter. Returns None for unknown."""
    et = event.get("event_type", "")
    content = event.get("content", "")
    if et == "assistant_text":
        return format_text_delta(content, model, request_uuid)
    if et == "thinking":
        return format_thinking_delta(content, model, request_uuid)
    if et == "tool_use":
        return format_tool_use_delta(content, model, request_uuid)
    if et == "tool_result":
        return format_tool_result_delta(content, model, request_uuid)
    return None


def build_openai_stream_chunks(result: dict, model: str, request_uuid: str) -> str:
    """LEGACY single-shot streaming used as fallback when real streaming is off.

    Emits the entire result as one content chunk + stop + [DONE]. Kept for
    callers that still rely on the old fake-streaming shape.
    """
    content = result.get("result", "")
    return (
        format_role_open_chunk(model, request_uuid)
        + format_text_delta(content, model, request_uuid)
        + format_stop_chunk(result.get("usage", {}), model, request_uuid)
        + format_done()
    )


def extract_request_id(headers: dict | None, body: dict) -> str:
    """Extract or generate a request UUID."""
    if headers:
        for key in ("x-request-id", "x_request_id"):
            if key in headers:
                return headers[key]
    return str(uuid4())
