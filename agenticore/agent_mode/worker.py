"""Standalone queue worker for async completions.

Runnable as: python -m agenticore.agent_mode.worker
Or via: python -m agenticore.agent_mode

Pops completion requests from the Redis queue and processes them
by spawning Claude subprocesses through AgentExecutor.
"""

import asyncio
import logging
import os
import sys

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _process_completion(request: dict) -> None:
    """Process a single completion request from the queue."""
    from agenticore.agent_mode.agent import AgentExecutor
    from agenticore.agent_mode.completions import update_completion
    from agenticore.agent_mode.notifications import send_notification

    uuid = request.get("uuid", "")
    callback_url = request.get("callback_url", "")

    _log.info("Processing completion: %s", uuid)

    update_completion(uuid, status="running", started_at=_now_iso())
    await send_notification(uuid, "status", {"status": "started", "uuid": uuid})

    executor = AgentExecutor()
    params = request.get("request_params", {})

    try:
        result = await executor.execute(
            message=request.get("message", ""),
            external_uuid=uuid,
            wait=True,
            **params,
        )

        is_error = result.get("is_error", False)
        update_completion(
            uuid,
            status="failed" if is_error else "completed",
            ended_at=_now_iso(),
            result=result.get("result", ""),
            session_id=result.get("session_id", ""),
            cost_usd=result.get("cost_usd", 0.0),
            duration_ms=result.get("duration_ms", 0),
            num_turns=result.get("num_turns", 0),
            is_error=is_error,
            error=result.get("error", ""),
            usage=result.get("usage", {}),
            tool_uses=result.get("tool_uses", []),
        )

        status_event = "completed" if not is_error else "failed"
        await send_notification(uuid, "status", {"status": status_event, "uuid": uuid})

        # Deliver final result
        if callback_url:
            await send_notification(uuid, "result", result)

    except Exception as e:
        _log.error("Completion %s failed: %s", uuid, e)
        update_completion(uuid, status="failed", error=str(e), ended_at=_now_iso())
        await send_notification(uuid, "status", {"status": "failed", "uuid": uuid, "error": str(e)})


async def completion_worker(max_concurrent: int = 1) -> None:
    """Main worker loop. BRPOP from Redis queue, process completions.

    Args:
        max_concurrent: Max concurrent completions to process.
    """
    from agenticore.agent_mode.completions import dequeue_completion

    sem = asyncio.Semaphore(max_concurrent)
    _log.info("Completion worker started (max_concurrent=%d)", max_concurrent)

    while True:
        request = dequeue_completion(timeout=5)
        if request is None:
            continue

        uuid = request.get("uuid", "unknown")
        _log.info("Dequeued completion: %s", uuid)

        async def _run(req):
            async with sem:
                await _process_completion(req)

        asyncio.create_task(_run(request))


async def run_inline_completion(request: dict) -> None:
    """Process a completion inline (no Redis queue).

    Used when Redis is unavailable — the server calls this directly
    instead of enqueueing.
    """
    await _process_completion(request)


def main():
    """Entry point: python -m agenticore.agent_mode.worker"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    from agenticore.config import get_config

    cfg = get_config()
    if not cfg.agent_mode.enabled:
        print("AGENT_MODE is not enabled. Set AGENT_MODE=true to start the worker.", file=sys.stderr)
        sys.exit(1)

    max_workers = cfg.agent_mode.max_queue_workers
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        print("REDIS_URL is required for the queue worker.", file=sys.stderr)
        sys.exit(1)

    print(f"Starting completion worker (max_concurrent={max_workers})...", file=sys.stderr)
    asyncio.run(completion_worker(max_concurrent=max_workers))


if __name__ == "__main__":
    main()
