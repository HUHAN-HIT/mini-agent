"""Core gateway abstractions: SessionSource, MessageEvent, SendResult, adapter contract.

These mirror hermes' boundary (see docs/im-gateway-design.md §5.1) but trimmed
to what mini-agent's P0/P1 needs: text-only DM, multi-app wecom, single-account
weixin iLink. Group/thread fields exist so future platforms don't force a
SessionSource rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    MIXED = "mixed"


@dataclass(frozen=True)
class SessionSource:
    """Where an inbound message came from.

    Frozen so it can be safely used as a routing/log key and shared between
    adapter -> runner -> delivery without accidental mutation. The runner only
    reads these fields; adapters that need mutable protocol state keep it
    internally and rebuild a fresh SessionSource per event.
    """

    platform: str
    chat_id: str
    chat_type: str = "dm"
    user_id: str = ""
    user_name: str = ""
    account_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    raw_chat_id: str = ""


@dataclass
class MessageEvent:
    """Normalized inbound message from a platform adapter."""

    text: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    message_id: str = ""
    raw_message: Any = None
    media_paths: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    internal: bool = False


@dataclass
class SendResult:
    """Result of sending a message to a platform."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    continuation_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformCapabilities:
    max_message_length: int = 4000
    supports_message_editing: bool = False
    supports_code_blocks: bool = False
    supports_typing: bool = False
    supports_media_upload: bool = False
    requires_context_token: bool = False
    final_only: bool = True


MessageHandler = Callable[[MessageEvent], Awaitable[None]]


class BasePlatformAdapter(ABC):
    """Minimal hermes-style platform adapter contract.

    Lifecycle: ``set_message_handler`` -> ``connect`` -> (events flow) ->
    ``disconnect``. Subclasses own all platform-specific state (tokens,
    sync cursors, dedup, rate-limit) — the runner never reaches into it.
    """

    platform_name: str = "base"
    capabilities: PlatformCapabilities = PlatformCapabilities()

    def __init__(self) -> None:
        self._message_handler: Optional[MessageHandler] = None
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._running: bool = False

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @property
    def fatal_error_code(self) -> Optional[str]:
        return self._fatal_error_code

    def mark_fatal(self, code: str, message: str) -> None:
        """Record a non-recoverable adapter error and stop processing new events."""
        self._fatal_error_code = code
        self._fatal_error_message = message

    async def _dispatch(self, event: MessageEvent) -> None:
        """Invoke the runner's message handler if registered."""
        if self._message_handler is None:
            return
        await self._message_handler(event)

    @abstractmethod
    async def connect(self) -> bool:
        """Connect/authenticate and start receiving messages.

        Returns True if the adapter is healthy and ready to receive events.
        Returning False (or calling ``mark_fatal``) signals the runner to skip
        this platform without crashing the whole gateway.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop receiving messages and release resources."""

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send content to a platform chat.

        ``metadata`` is the channel for adapter-specific routing hints
        (account_id, user_id, raw_chat_id) that the runner carries from the
        inbound SessionSource.
        """

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Optional platform typing indicator. No-op by default."""
        return None
