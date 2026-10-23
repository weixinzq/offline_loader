"""Bounded, best-effort notifications; persisted queries remain authoritative."""
import asyncio
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class EventHub:
    def __init__(self, queue_size=256):
        self.queue_size = queue_size
        self._subscribers = set()
        self._sequence = 0

    def subscribe(self):
        queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    def publish(self, name, data):
        self._sequence += 1
        event = {"id": str(self._sequence), "event": name, "timestamp": utc_now(), "data": data}
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        return event
