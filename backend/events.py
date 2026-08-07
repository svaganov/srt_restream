"""In-memory event bus for the UI events panel.

Recent events are kept in a bounded ring buffer and pushed to subscribed
WebSocket clients. Nothing is persisted; history survives only the process
lifetime (DB persistence is a separate phase).
"""
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional


class EventBus:
    def __init__(self, maxlen: int = 1000):
        self._events = deque(maxlen=maxlen)
        self._subscribers: list[Callable] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def emit(
        self,
        level: str,
        category: str,
        event: str,
        message: str,
        stream_id: Optional[int] = None,
        stream_name: Optional[str] = None,
        source: str = "system",
    ) -> dict:
        with self._lock:
            item = {
                "id": self._next_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "category": category,
                "event": event,
                "stream_id": stream_id,
                "stream_name": stream_name,
                "source": source,
                "message": message,
            }
            self._next_id += 1
            self._events.append(item)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(item)
            except Exception:
                pass
        return item

    def list(self, limit: int = 200) -> list:
        """Most recent events, newest last."""
        with self._lock:
            items = list(self._events)
        return items[-limit:]

    def subscribe(self, callback: Callable) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)


event_bus = EventBus()
