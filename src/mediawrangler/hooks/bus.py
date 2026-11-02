"""Fan-out of events to the Live tab. Safe to call from any thread."""

import asyncio
from typing import Any


class LiveBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, item: dict[str, Any]) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._fan_out, item)
        else:
            self._fan_out(item)

    def _fan_out(self, item: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()  # a slow viewer loses its oldest event, not the stream
            queue.put_nowait(item)
