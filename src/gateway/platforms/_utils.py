"""Shared adapter utilities: TTL dedup, rate-limit circuit, text debouncer.

These are protocol-agnostic helpers used by both wecom and weixin adapters.
They are intentionally simple — hermes has richer versions, but P0/P1 only
needs the contract below.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional


class TtlSet:
    """Bounded TTL set for ``seen_or_mark`` dedup semantics.

    Used by wecom for inbound ``MsgId`` dedup and weixin for ``message_id`` and
    content-hash dedup. ``seen_or_mark(key)`` returns True if the key was
    already seen within TTL, else adds it and returns False.
    """

    def __init__(self, *, ttl_seconds: int = 300, max_size: int = 2000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: OrderedDict[str, float] = OrderedDict()

    def _evict(self, now: float) -> None:
        # Evict expired from the front (oldest first).
        while self._store:
            key, ts = next(iter(self._store.items()))
            if now - ts > self._ttl:
                self._store.popitem(last=False)
            else:
                break

    def seen(self, key: str) -> bool:
        now = time.monotonic()
        self._evict(now)
        return key in self._store

    def seen_or_mark(self, key: str) -> bool:
        if not key:
            return False
        now = time.monotonic()
        self._evict(now)
        if key in self._store:
            # Refresh recency without changing insert order semantics.
            self._store.move_to_end(key)
            self._store[key] = now
            return True
        self._store[key] = now
        if len(self._store) > self._max_size:
            self._store.popitem(last=False)
        return False


class MessageDeduplicator:
    """Thin wrapper over TtlSet with optional content hashing.

    Used by the weixin adapter where the same content from the same peer in a
    short window should be considered a replay (e.g. iLink sometimes re-sends).
    """

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self._set = TtlSet(ttl_seconds=ttl_seconds, max_size=10_000)

    def is_duplicate(self, key: str) -> bool:
        if not key:
            return False
        return self._set.seen_or_mark(key)

    @staticmethod
    def content_key(*parts: str) -> str:
        joined = "\x00".join(str(p) for p in parts)
        digest = hashlib.md5(joined.encode("utf-8")).hexdigest()
        return f"content:{digest}"


class RateLimitCircuit:
    """Tiny fixed-threshold circuit breaker.

    iLink sends ``errcode=-2`` on too-frequent sends. Once we see ``threshold``
    failures within ``window_seconds``, the circuit opens for
    ``open_seconds`` to give the platform breathing room.
    """

    def __init__(
        self,
        *,
        threshold: int = 1,
        window_seconds: int = 30,
        open_seconds: int = 30,
    ) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._open_for = open_seconds
        self._events: list[float] = []
        self._opened_until: float = 0.0

    def record(self, *, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.monotonic()
        self._events.append(ts)
        cutoff = ts - self._window
        self._events = [t for t in self._events if t >= cutoff]
        if len(self._events) >= self._threshold:
            self._opened_until = ts + self._open_for

    def is_open(self, *, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.monotonic()
        return ts < self._opened_until


class TextDebouncer:
    """Coalesce rapid-fire text messages from the same session.

    WeChat users often send 3 short messages in a row. Without debouncing each
    message would trigger a separate agent turn. The debouncer accumulates
    messages for the same key and flushes after ``delay_seconds`` of quiet, or
    after ``split_delay_seconds`` since the first message even if more arrive.
    """

    def __init__(
        self,
        *,
        delay_seconds: float,
        split_delay_seconds: float,
        flush: Callable[[str, list], Awaitable[None]],
    ) -> None:
        self._delay = delay_seconds
        self._split_delay = split_delay_seconds
        self._flush = flush
        self._buffers: dict[str, list] = {}
        self._first_ts: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, key: str, event: object) -> None:
        self._buffers.setdefault(key, []).append(event)
        now = time.monotonic()
        self._first_ts.setdefault(key, now)

        existing = self._tasks.get(key)
        if existing and not existing.done():
            return

        async def _wait() -> None:
            try:
                await asyncio.sleep(self._delay)
                await self._flush_key(key)
            except asyncio.CancelledError:
                # On close, drop silently; close() handles a final flush.
                raise

        self._tasks[key] = asyncio.create_task(_wait())

    async def _flush_key(self, key: str) -> None:
        events = self._buffers.pop(key, [])
        self._first_ts.pop(key, None)
        self._tasks.pop(key, None)
        if events:
            await self._flush(key, events)

    async def flush_all(self) -> None:
        keys = list(self._buffers.keys())
        for key in keys:
            task = self._tasks.get(key)
            if task and not task.done():
                task.cancel()
            await self._flush_key(key)

    async def close(self) -> None:
        await self.flush_all()


def hash_token(token: str) -> str:
    """SHA-256 short hash for logging / lock identity without leaking tokens."""

    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
