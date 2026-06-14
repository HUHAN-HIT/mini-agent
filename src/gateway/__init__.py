"""Mini-agent IM gateway package.

Adapters normalize IM platform events into MessageEvent and route them through
the runner, which serializes per-session agent turns and delivers the final
answer back through the originating adapter.
"""

from src.gateway.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageHandler,
    MessageType,
    PlatformCapabilities,
    SendResult,
    SessionSource,
)

__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "MessageHandler",
    "MessageType",
    "PlatformCapabilities",
    "SendResult",
    "SessionSource",
]
