"""WebSocket connection manager for real-time dashboard notifications."""

import asyncio
import json
from typing import Set

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, event: str, data: dict) -> None:
        msg = json.dumps({"event": event, "data": data})
        dead: Set[WebSocket] = set()
        async with self._lock:
            conns = list(self._connections)
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._connections -= dead


ws_manager = WebSocketManager()
