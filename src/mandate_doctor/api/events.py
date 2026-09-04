"""In-process event bus broadcasting pipeline steps to dashboard clients.

The collector (running as a background task inside the API process)
publishes events; the WebSocket endpoint fans them out to every
connected browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)


class EventSink(Protocol):
    """Anything the collector can publish events to."""

    async def publish(self, event: dict[str, Any]) -> None: ...


class EventBus:
    """Fan-out hub with per-client queues (slow clients never block the
    pipeline; overflowing clients are dropped)."""

    def __init__(self, max_queue: int = 500) -> None:
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self._max_queue = max_queue
        self._lock = asyncio.Lock()

    async def subscribe(self) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        async with self._lock:
            self._next_id += 1
            client_id = self._next_id
            self._subscribers[client_id] = asyncio.Queue(maxsize=self._max_queue)
            return client_id, self._subscribers[client_id]

    async def unsubscribe(self, client_id: int) -> None:
        async with self._lock:
            self._subscribers.pop(client_id, None)

    async def publish(self, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        async with self._lock:
            queues = list(self._subscribers.values())
        for q in queues:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


bus = EventBus()


class PrintSink:
    """CLI-mode sink: prints events as one-line JSON."""

    async def publish(self, event: dict[str, Any]) -> None:
        logger.info("pipeline_event", **event)
