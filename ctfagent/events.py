"""Event bus: SQLite rows are the truth, an in-process fan-out wakes SSE clients.

Mirrors the one pattern worth taking from ARTEX's ``broadcast.go``: publish is
non-blocking and lossy; a subscriber that falls behind re-reads from its last
id. That is what makes ``Last-Event-ID`` reconnect work without keeping every
event in memory.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from .db import Store
from .models import Event


class EventBus:
    def __init__(self, store: Store, queue_size: int = 256) -> None:
        self.store = store
        self.queue_size = queue_size
        self._subs: dict[int, set[asyncio.Queue[Event]]] = defaultdict(set)
        #: Subscribers that want every run (the workspace's task rail). One
        #: connection for the whole page instead of one SSE stream per run.
        self._all: set[asyncio.Queue[Event]] = set()

    def publish(self, run_id: int, kind: str, payload: dict[str, Any], task_id: int | None = None) -> Event:
        event = self.store.add_event(run_id, kind, payload, task_id=task_id)
        self._fan_out(run_id, event)
        return event

    def notify(
        self, run_id: int, kind: str, payload: dict[str, Any], task_id: int | None = None
    ) -> None:
        """Fan out an event without persisting it.

        Used for token-by-token text, which is only meaningful while someone is
        watching. The durable record is the finished message written on
        ``result``; replaying a thousand deltas after a reload would be noise.
        An id of 0 marks the event as live-only for the SSE cursor.
        """
        event = Event(
            id=0, run_id=run_id, task_id=task_id, kind=kind, payload=payload, created_at=""
        )
        self._fan_out(run_id, event)

    def _fan_out(self, run_id: int, event: Event) -> None:
        for queue in list(self._subs.get(run_id, ())) + list(self._all):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The subscriber will notice the gap and re-read by id.
                pass

    def subscribe(self, run_id: int) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.queue_size)
        self._subs[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: int, queue: asyncio.Queue[Event]) -> None:
        self._subs.get(run_id, set()).discard(queue)

    def subscribe_all(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.queue_size)
        self._all.add(queue)
        return queue

    def unsubscribe_all(self, queue: asyncio.Queue[Event]) -> None:
        self._all.discard(queue)
