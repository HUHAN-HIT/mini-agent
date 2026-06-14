"""Tests for LLMResponse.cache_stats parsing (Phase 2).

AC-002 (OpenAI cached_tokens parsed, ratio >= 0.80 from turn 2):
    Run a scripted multi-turn conversation (>=3 turns) against a provider whose
    response_metadata contains token_usage.prompt_tokens_details.cached_tokens.
    From turn 2 onward, parsed cache_stats.cache_hit_ratio >= 0.80.

AC-009 (stream fallback parses cache_stats):
    Inject a fake bound LLM whose .stream() raises RuntimeError, whose .invoke()
    returns an AIMessage-like object with response_metadata.token_usage.
    prompt_tokens_details.cached_tokens = 100 and prompt_tokens = 200. Call
    ChatLLM.stream_chat(...). The returned LLMResponse.cache_stats must be
    populated with cached_tokens=100, prompt_tokens=200, cache_hit_ratio=0.5.

AC-010 (missing usage -> cache_stats.is_available == False):
    Parse an AIMessage whose response_metadata has no 'token_usage' and no 'usage'
    keys. cache_stats.is_available == False, no exception raised.

These tests FAIL today because:
  - `LLMResponse` in src/providers/chat.py has no `cache_stats` attribute.
    `ChatLLM._parse_response` does not parse token_usage / cached_tokens.
    Accessing `response.cache_stats` raises AttributeError. This is the
    "right reason" failure: the feature being added is what would make the
    attribute exist.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


class FakeAIMessage:
    """Mimics a langchain AIMessage — content, additional_kwargs, tool_calls,
    response_metadata."""

    def __init__(
        self,
        content: str = "answer",
        response_metadata: Optional[Dict[str, Any]] = None,
        additional_kwargs: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.content = content
        self.response_metadata = response_metadata or {}
        self.additional_kwargs = additional_kwargs or {}
        self.tool_calls = tool_calls or []


class FakeBoundLLM:
    """Fake of langchain's bound LLM: configurable stream + invoke behavior."""

    def __init__(
        self,
        invoke_return: FakeAIMessage,
        stream_raises: Optional[Exception] = None,
    ) -> None:
        self._invoke_return = invoke_return
        self._stream_raises = stream_raises

    def stream(self, messages, config=None):
        if self._stream_raises is not None:
            raise self._stream_raises
        return iter([])  # not used in fallback tests

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        return self._invoke_return


def _build_chat_llm_with_bound(bound_llm: FakeBoundLLM):
    """Construct a ChatLLM whose underlying _llm is the given fake bound LLM."""
    from src.providers import chat as chat_mod

    llm = chat_mod.ChatLLM.__new__(chat_mod.ChatLLM)
    llm.model_name = "fake-1"
    llm._llm = bound_llm
    return llm


def test_openai_cached_tokens_parsed_ratio_threshold() -> bool:
    """AC-002: scripted 3-turn conversation; turn 2+ ratios >= 0.80."""
    print("\n=== TEST AC-002: OpenAI cached_tokens parsed, ratio >= 0.80 from turn 2 ===")

    # Scripted responses: turn 1 cold (no cached), turns 2-3 with >= 80% cached.
    responses = [
        FakeAIMessage(
            content=f"answer-{i}",
            response_metadata={
                "token_usage": {
                    "prompt_tokens": prompt,
                    "prompt_tokens_details": {"cached_tokens": cached},
                },
                "finish_reason": "stop",
            },
        )
        for i, (prompt, cached) in enumerate([(1000, 0), (5000, 4200), (5200, 4500)], start=1)
    ]

    ok = True
    ratios = []
    for turn_idx, ai_msg in enumerate(responses, start=1):
        bound = FakeBoundLLM(invoke_return=ai_msg)
        llm = _build_chat_llm_with_bound(bound)
        # Use chat() to exercise _parse_response directly
        resp = llm.chat(messages=[{"role": "user", "content": "x"}])

        # cache_stats attribute must exist
        if not hasattr(resp, "cache_stats"):
            print(f"  FAIL (turn {turn_idx}): LLMResponse has no 'cache_stats' attribute — "
                  f"feature not implemented yet (Phase 2 work).")
            ok = False
            ratios.append(None)
            continue

        cs = resp.cache_stats
        if turn_idx == 1:
            # Turn 1: any ratio (including None) is acceptable per AC-002.
            print(f"  turn 1: ratio={getattr(cs, 'cache_hit_ratio', None)} (any allowed)")
            ratios.append(getattr(cs, "cache_hit_ratio", None))
            continue

        if not cs.is_available:
            print(f"  FAIL (turn {turn_idx}): cache_stats.is_available is False; expected True")
            ok = False
            ratios.append(None)
            continue

        ratio = cs.cache_hit_ratio
        ratios.append(ratio)
        if ratio is None or ratio < 0.80:
            print(f"  FAIL (turn {turn_idx}): cache_hit_ratio={ratio}, expected >= 0.80")
            ok = False
        else:
            print(f"  OK (turn {turn_idx}): cache_hit_ratio={ratio:.4f} >= 0.80")

    if ok:
        print(f"  ratios observed: {ratios}")
    if not ok:
        return False
    assert ok, "OpenAI cached_tokens parsed with ratio >= 0.80 from turn 2"
    return True


def test_stream_fallback_parses_cache_stats() -> bool:
    """AC-009: when stream() raises, fallback to chat() still populates cache_stats."""
    print("\n=== TEST AC-009: stream fallback parses cache_stats ===")

    ai_msg = FakeAIMessage(
        content="answer",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
            "finish_reason": "stop",
        },
    )
    bound = FakeBoundLLM(invoke_return=ai_msg, stream_raises=RuntimeError("simulated stream failure"))
    llm = _build_chat_llm_with_bound(bound)

    resp = llm.stream_chat(messages=[{"role": "user", "content": "x"}])

    if not hasattr(resp, "cache_stats"):
        print("  FAIL: LLMResponse has no 'cache_stats' attribute after stream fallback — "
              "feature not implemented yet (Phase 2 work).")
        return False

    cs = resp.cache_stats
    ok = True
    if not cs.is_available:
        print(f"  FAIL: cache_stats.is_available = False; expected True")
        ok = False
    else:
        print("  OK: cache_stats.is_available = True")

    if cs.cached_tokens != 100:
        print(f"  FAIL: cache_stats.cached_tokens = {cs.cached_tokens!r}, expected 100")
        ok = False
    else:
        print(f"  OK: cache_stats.cached_tokens = 100")

    if cs.cache_hit_ratio != 0.5:
        print(f"  FAIL: cache_stats.cache_hit_ratio = {cs.cache_hit_ratio!r}, expected 0.5")
        ok = False
    else:
        print(f"  OK: cache_stats.cache_hit_ratio = 0.5")

    if not ok:
        return False
    assert ok, "stream fallback parses cache_stats"
    return True


def test_missing_usage_returns_unavailable_cache_stats() -> bool:
    """AC-010: missing usage block -> cache_stats.is_available False, no exception."""
    print("\n=== TEST AC-010: missing usage -> cache_stats.is_available = False ===")

    ai_msg = FakeAIMessage(
        content="answer",
        response_metadata={"finish_reason": "stop"},  # no token_usage, no usage
    )
    bound = FakeBoundLLM(invoke_return=ai_msg)
    llm = _build_chat_llm_with_bound(bound)

    try:
        resp = llm.chat(messages=[{"role": "user", "content": "x"}])
    except Exception as exc:
        print(f"  FAIL: chat() raised {type(exc).__name__}: {exc} — expected no exception")
        return False

    if not hasattr(resp, "cache_stats"):
        print("  FAIL: LLMResponse has no 'cache_stats' attribute — feature not implemented yet.")
        return False

    cs = resp.cache_stats
    if cs.is_available:
        print(f"  FAIL: cache_stats.is_available = True; expected False")
        return False

    if cs.prompt_tokens is not None:
        print(f"  FAIL: cache_stats.prompt_tokens = {cs.prompt_tokens!r}, expected None")
        return False
    if cs.cached_tokens is not None:
        print(f"  FAIL: cache_stats.cached_tokens = {cs.cached_tokens!r}, expected None")
        return False
    if cs.cache_hit_ratio is not None:
        print(f"  FAIL: cache_stats.cache_hit_ratio = {cs.cache_hit_ratio!r}, expected None")
        return False

    assert cs.is_available is False, "missing usage → cache_stats unavailable"
    print("  OK: cache_stats.is_available = False, all fields None, no exception raised")
    return True


def main() -> None:
    results = [
        test_openai_cached_tokens_parsed_ratio_threshold(),
        test_stream_fallback_parses_cache_stats(),
        test_missing_usage_returns_unavailable_cache_stats(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
