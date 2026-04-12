"""Async generator that tails the per-completion Redis Stream of hook events.

Reads from `agenticore:events:{uuid}` (XREAD BLOCK in an executor since redis-py
is sync), filters by stream_config visibility, and yields raw event dicts to
the caller. Stops on the `done` sentinel, on subprocess exit, or on a hard
timeout. Designed to feed an SSE generator that maps each event to a delta
chunk.
"""

import asyncio
import json
import os
from typing import AsyncGenerator, Optional

from agenticore.jobs import _get_redis
from agenticore.agent_mode.stream_config import is_visible

KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "agenticore")
XREAD_BLOCK_MS = 500
WATCHDOG_GRACE_POLLS = 4
DEFAULT_TIMEOUT_S = 600


def stream_key(uuid: str) -> str:
    return f"{KEY_PREFIX}:events:{uuid}"


def _decode(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def parse_stream_entry(entry_id, fields: dict) -> dict:
    """Convert a raw XREAD entry into the in-memory event dict."""
    decoded = {_decode(k): _decode(v) for k, v in fields.items()}
    return {
        "id": _decode(entry_id),
        "event_type": decoded.get("event_type", "unknown"),
        "content": decoded.get("content", ""),
        "ts": decoded.get("ts", ""),
        "session_id": decoded.get("session_id", ""),
        "correlation_id": decoded.get("correlation_id", ""),
    }


def _xread_sync(redis_client, key: str, last_id: str, block_ms: int):
    """Sync XREAD wrapper for run_in_executor."""
    try:
        return redis_client.xread({key: last_id}, count=100, block=block_ms)
    except Exception:
        return None


async def tail_event_stream(
    uuid: str,
    stream_cfg: dict[str, bool],
    process_handle: Optional[asyncio.subprocess.Process] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> AsyncGenerator[dict, None]:
    """Yields filtered event dicts from agenticore:events:{uuid}.

    Starts at last_id "0-0" so events emitted before the tailer attached
    are replayed. Stops on done sentinel, process exit + grace, or timeout.
    """
    r = _get_redis()
    if r is None:
        return
    key = stream_key(uuid)
    last_id = "0-0"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    grace_polls_remaining = WATCHDOG_GRACE_POLLS
    saw_done = False

    while True:
        if loop.time() > deadline:
            return
        result = await loop.run_in_executor(None, _xread_sync, r, key, last_id, XREAD_BLOCK_MS)
        if result:
            for _stream, entries in result:
                for entry_id, fields in entries:
                    event = parse_stream_entry(entry_id, fields)
                    last_id = event["id"]
                    if event["event_type"] == "done":
                        yield event
                        saw_done = True
                        break
                    if is_visible(event["event_type"], stream_cfg):
                        yield event
                if saw_done:
                    break
            if saw_done:
                return
            grace_polls_remaining = WATCHDOG_GRACE_POLLS
            continue
        if process_handle is not None and process_handle.returncode is not None:
            grace_polls_remaining -= 1
            if grace_polls_remaining <= 0:
                return
        await asyncio.sleep(0.05)


async def tail_event_stream_until_done(
    uuid: str,
    stream_cfg: dict[str, bool],
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Convenience for tests: collect all visible events until done sentinel."""
    out = []
    async for event in tail_event_stream(uuid, stream_cfg, None, timeout_s):
        out.append(event)
        if event["event_type"] == "done":
            break
    return out
