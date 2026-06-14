"""Final-only delivery with capability-driven chunking.

P0/P1 only sends the agent's final answer back to the originating chat. The
adapter's ``capabilities.max_message_length`` decides how to split long output;
each chunk is sent in order and any failure short-circuits the rest.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.gateway.base import BasePlatformAdapter, SendResult, SessionSource

logger = logging.getLogger(__name__)


def split_message(
    content: str,
    *,
    max_length: int,
    len_fn=len,
) -> list[str]:
    """Split content into chunks no longer than ``max_length``.

    Tries to break on blank lines, then newlines, then spaces, then anywhere.
    Returns ``[""]`` if content is empty so callers always send at least one
    chunk (the runner's "(无回复)" placeholder is the caller's responsibility).
    """

    if max_length <= 0:
        return [content]
    if not content:
        return [""]
    if len_fn(content) <= max_length:
        return [content]

    chunks: list[str] = []
    remaining = content
    while len_fn(remaining) > max_length:
        window = remaining[:max_length]

        cut = _best_break(window, "\n\n")
        if cut <= 0:
            cut = _best_break(window, "\n")
        if cut <= 0:
            cut = _best_break(window, " ")
        if cut <= 0:
            cut = max_length

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def _best_break(window: str, sep: str) -> int:
    """Return the index after the last ``sep`` in window, or 0 if none."""

    pos = window.rfind(sep)
    if pos <= 0:
        return 0
    return pos + len(sep)


async def deliver_final_response(
    *,
    adapter: BasePlatformAdapter,
    source: SessionSource,
    content: str,
    reply_to: Optional[str],
) -> SendResult:
    """Send a final agent reply, chunked to the adapter's max length.

    Sends metadata that adapters commonly need: account_id (multi-app wecom),
    user_id (weixin context_token lookup), chat_type and raw_chat_id for
    logging/scope. The adapter picks what it needs and ignores the rest.
    """

    chunks = split_message(
        content,
        max_length=adapter.capabilities.max_message_length,
    )

    metadata = {
        "account_id": source.account_id,
        "user_id": source.user_id,
        "chat_type": source.chat_type,
        "raw_chat_id": source.raw_chat_id,
        "thread_id": source.thread_id,
    }

    last_result = SendResult(success=True)
    continuation: list[str] = []
    for index, chunk in enumerate(chunks):
        result = await adapter.send(
            source.chat_id,
            chunk,
            reply_to=reply_to,
            metadata=metadata,
        )
        if not result.success:
            logger.warning(
                "delivery chunk %d/%d failed on %s: %s",
                index + 1,
                len(chunks),
                adapter.platform_name,
                result.error,
            )
            return result
        last_result = result
        if result.message_id:
            continuation.append(result.message_id)

    if len(continuation) > 1:
        return SendResult(
            success=True,
            message_id=continuation[-1] if continuation else None,
            continuation_message_ids=tuple(continuation[:-1]) if len(continuation) > 1 else (),
            raw_response=last_result.raw_response,
        )
    return last_result
