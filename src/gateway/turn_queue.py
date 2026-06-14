"""Per-session turn serialization.

The runner schedules one task per inbound message, but mini-agent history is
append-only and an in-flight turn mutates shared session state. Two concurrent
turns in the same session would race on history and confuse the agent.

P0 uses one ``asyncio.Lock`` per session_id. If we later want to surface
queue depth for typing indicators, the lock can be upgraded to a real queue.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class SessionTurnQueue:
    """Serialize agent turns per mini-agent session."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def run(self, session_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        async with self._lock_for(session_id):
            await fn()

    def pending(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return bool(lock and lock.locked())
