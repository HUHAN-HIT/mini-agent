"""SSE event bus with support for last_event_id recovery and buffering."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class SSEEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    event_type: str = "message"
    data: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False)
        return "\n".join([f"id: {self.event_id}", f"event: {self.event_type}", f"data: {payload}", "", ""])


class EventBus:
    """Session-scoped event bus with subscribers and buffered events."""

    def __init__(self, max_buffer_size: int = 500) -> None:
        self.max_buffer_size = max_buffer_size
        self._buffers: Dict[str, List[SSEEvent]] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: SSEEvent) -> None:
        session_id = event.session_id
        with self._lock:
            if session_id not in self._buffers:
                self._buffers[session_id] = []
            buffer = self._buffers[session_id]
            buffer.append(event)
            if len(buffer) > self.max_buffer_size:
                self._buffers[session_id] = buffer[-self.max_buffer_size:]
            queues = list(self._subscribers.get(session_id, []))
        for queue in queues:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._safe_put, queue, event)
            else:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def _safe_put(queue: asyncio.Queue, event: SSEEvent) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def emit(self, session_id: str, event_type: str, data: Optional[Dict[str, Any]] = None) -> SSEEvent:
        event = SSEEvent(event_type=event_type, data=data or {}, session_id=session_id)
        self.publish(event)
        return event

    def replay(self, session_id: str, last_event_id: Optional[str] = None) -> List[SSEEvent]:
        if not last_event_id:
            return []
        with self._lock:
            buffer = self._buffers.get(session_id, [])
            found = False
            result: List[SSEEvent] = []
            for event in buffer:
                if found:
                    result.append(event)
                elif event.event_id == last_event_id:
                    found = True
            return result

    async def subscribe(self, session_id: str, last_event_id: Optional[str] = None) -> AsyncIterator[SSEEvent]:
        queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=200)
        with self._lock:
            if session_id not in self._subscribers:
                self._subscribers[session_id] = []
            self._subscribers[session_id].append(queue)
        try:
            replay_events = self.replay(session_id, last_event_id)
            for event in replay_events:
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    yield SSEEvent(event_type="heartbeat", data={"ts": time.time()}, session_id=session_id)
        finally:
            with self._lock:
                subs = self._subscribers.get(session_id, [])
                if queue in subs:
                    subs.remove(queue)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._buffers.pop(session_id, None)
