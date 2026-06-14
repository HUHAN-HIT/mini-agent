"""ChatLLM: raw LLM message interface with function calling support."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.providers.llm import build_llm

logger = logging.getLogger(__name__)


def _dedupe_finish_reason(raw: str) -> str:
    return next(
        (m for m in ("tool_calls", "function_call", "content_filter", "length", "stop")
         if raw.endswith(m)),
        raw,
    )


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class CacheStats:
    """Cache-observability data parsed from provider response_metadata.

    Per clarification Q4 + OQ-001 option A:
      - prompt_tokens / cached_tokens are the raw ints when the provider reports
        them; None otherwise.
      - cache_hit_ratio = cached_tokens / prompt_tokens when both are ints and
        prompt_tokens > 0; None otherwise (NOT 0.0 — keeps the rolling-window
        cache_warning from mis-firing on missing usage).
      - cache_write_tokens is Anthropic's cache_creation_input_tokens surfaced
        separately for observability; EXCLUDED from the hit ratio (writing the
        cache costs money; AC-002 should not trivially pass on turn 1).
      - is_available is True only when cache_hit_ratio is a number.
    """
    prompt_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    cache_hit_ratio: Optional[float] = None
    cache_write_tokens: Optional[int] = None

    @property
    def is_available(self) -> bool:
        return self.cache_hit_ratio is not None


@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    finish_reason: str = "stop"
    cache_stats: CacheStats = field(default_factory=CacheStats)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class ChatLLM:
    """LLM chat client with function calling support."""

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name
        self._llm = build_llm(model_name=model_name)

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, timeout: Optional[int] = None) -> LLMResponse:
        llm = self._llm.bind_tools(tools) if tools else self._llm
        config = {"timeout": timeout} if timeout else {}
        ai_message = llm.invoke(messages, config=config)
        return self._parse_response(ai_message)

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_text_chunk: Optional[Any] = None,
        on_reasoning_chunk: Optional[Any] = None,
        timeout: Optional[int] = None,
    ) -> LLMResponse:
        try:
            llm = self._llm.bind_tools(tools) if tools else self._llm
            config = {"timeout": timeout} if timeout else {}
            accumulated = None
            for chunk in llm.stream(messages, config=config):
                if chunk.content and on_text_chunk:
                    on_text_chunk(chunk.content)
                reasoning = ""
                if chunk.additional_kwargs:
                    reasoning = chunk.additional_kwargs.get("reasoning_content") or ""
                if reasoning and on_reasoning_chunk:
                    on_reasoning_chunk(reasoning)
                accumulated = chunk if accumulated is None else accumulated + chunk
            if accumulated is None:
                return LLMResponse(content="", tool_calls=[], finish_reason="stop")
            return self._parse_response(accumulated)
        except Exception:
            logger.exception("stream_chat failed; falling back to non-streaming chat")
            return self.chat(messages, tools=tools, timeout=timeout)

    async def achat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, timeout: Optional[int] = None) -> LLMResponse:
        llm = self._llm.bind_tools(tools) if tools else self._llm
        config = {"timeout": timeout} if timeout else {}
        ai_message = await llm.ainvoke(messages, config=config)
        return self._parse_response(ai_message)

    @staticmethod
    def _parse_response(ai_message: Any) -> LLMResponse:
        response_metadata = getattr(ai_message, "response_metadata", None) or {}

        return LLMResponse(
            content=ai_message.content,
            tool_calls=[
                ToolCallRequest(id=tc["id"], name=tc["name"], arguments=tc["args"])
                for tc in ai_message.tool_calls
            ],
            reasoning_content=ai_message.additional_kwargs.get("reasoning_content"),
            finish_reason=_dedupe_finish_reason(
                response_metadata.get("finish_reason", "stop")
            ),
            cache_stats=CacheStats._from_metadata(response_metadata),
        )


def _safe_int(value: Any) -> Optional[int]:
    """Coerce to int when possible; None otherwise. Avoids bool-truthiness traps."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # Some providers return strings; try to parse.
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    return None


def _safe_get(mapping: Any, *keys: Any, default: Any = None) -> Any:
    """Traverse nested dicts/tuples by key chain; return default on any miss."""
    cur = mapping
    for key in keys:
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[key]
            except (IndexError, TypeError):
                return default
        else:
            return default
    return cur


def _parse_cache_stats(metadata: Dict[str, Any]) -> CacheStats:
    """Extract CacheStats from provider response_metadata.

    Tries each provider format in turn; first hit wins. Any missing field
    leaves the default None. NO KeyError/TypeError/ZeroDivisionError escapes.
    See clarification Q5 for the field-path matrix.
    """
    prompt_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None

    try:
        # 1. OpenAI / DeepSeek style: token_usage.prompt_tokens_details.cached_tokens
        token_usage = _safe_get(metadata, "token_usage", default=None)
        if isinstance(token_usage, dict):
            prompt_tokens = _safe_int(token_usage.get("prompt_tokens"))
            details = _safe_get(token_usage, "prompt_tokens_details", default=None)
            if isinstance(details, dict):
                cached_tokens = _safe_int(details.get("cached_tokens"))

        # 2. Zhipu style: usage.cached_tokens (separate from token_usage above)
        if cached_tokens is None:
            usage = _safe_get(metadata, "usage", default=None)
            if isinstance(usage, dict):
                if prompt_tokens is None:
                    prompt_tokens = _safe_int(usage.get("prompt_tokens"))
                cached_tokens = _safe_int(usage.get("cached_tokens"))

        # 3. Anthropic / OpenRouter-Anthropic style: cache_read_input_tokens +
        #    cache_creation_input_tokens at the top level of response_metadata.
        #    prompt_tokens lives in usage.input_tokens for this provider.
        if cached_tokens is None:
            cache_read = _safe_get(metadata, "cache_read_input_tokens", default=None)
            if cache_read is not None:
                cached_tokens = _safe_int(cache_read)
                cache_write_tokens = _safe_int(
                    _safe_get(metadata, "cache_creation_input_tokens", default=None)
                )
                usage = _safe_get(metadata, "usage", default=None)
                if isinstance(usage, dict):
                    if prompt_tokens is None:
                        prompt_tokens = _safe_int(usage.get("input_tokens"))
    except Exception:
        # Defensive: any unexpected shape leaves fields at None.
        return CacheStats()

    # Compute ratio only when both inputs are ints and prompt > 0.
    ratio: Optional[float] = None
    if isinstance(prompt_tokens, int) and isinstance(cached_tokens, int) and prompt_tokens > 0:
        ratio = cached_tokens / prompt_tokens

    return CacheStats(
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        cache_hit_ratio=ratio,
        cache_write_tokens=cache_write_tokens,
    )


# Attach the module-level helper to CacheStats so the existing call site
# `CacheStats._from_metadata(...)` keeps working without circular imports.
CacheStats._from_metadata = staticmethod(_parse_cache_stats)  # type: ignore[attr-defined]
