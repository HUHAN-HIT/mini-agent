"""Integration test for long-conversation cache stability (Phase 2, AC-008).

AC-008 (long-conversation ratio >= 0.70 over 20 turns):
    Run a scripted 20-turn conversation against an OpenAI/DeepSeek-style
    provider that returns cached_tokens growing with prefix stability.
    Compute mean(cache_hit_ratio for turns 2..20). Mean must be >= 0.70.

Today (RED):
    LLMResponse has no cache_stats attribute; AgentLoop never emits per-turn
    cache_stats. So the integration test cannot observe any ratio — fails
    for the right reason (missing observability).
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


def _make_growing_cache_llm(turns: int):
    """Scripted LLM whose stream_chat returns growing cached_tokens.

    The simulated provider returns:
      - turn 1: cold (cached=0, ratio=0)
      - turns 2..N: cached_tokens grows to ~85% of prompt_tokens (stable prefix)
    """
    from src.providers import chat as chat_mod

    class _Shim:
        """Lightweight cache_stats stand-in (production CacheStats doesn't exist yet)."""
        def __init__(self, prompt_tokens: Optional[int], cached_tokens: Optional[int]) -> None:
            self.prompt_tokens = prompt_tokens
            self.cached_tokens = cached_tokens
            self.cache_hit_ratio = (
                cached_tokens / prompt_tokens
                if prompt_tokens and cached_tokens is not None and prompt_tokens > 0
                else None
            )

        @property
        def is_available(self) -> bool:
            return self.cache_hit_ratio is not None

    class _ScriptedLLM:
        def __init__(self) -> None:
            self._call = 0
            # Each turn's prompt grows because history accumulates.
            # Cached grows proportionally — assume prefix is stable.
            self._ratios_per_turn = self._plan_ratios(turns)

        @staticmethod
        def _plan_ratios(n: int) -> List[float]:
            # Turn 1: 0.0 (cold). Turns 2..N: 0.85 (stable prefix, 85% cached).
            return [0.0] + [0.85] * (n - 1)

        def stream_chat(self, messages, tools=None, on_text_chunk=None,
                        on_reasoning_chunk=None, timeout=None):
            self._call += 1
            idx = min(self._call - 1, turns - 1)
            # Simulate prompt_tokens growing with conversation length.
            prompt_tokens = 2000 + idx * 500  # ~2K base + 500/turn
            ratio = self._ratios_per_turn[idx]
            cached_tokens = int(prompt_tokens * ratio)

            resp = chat_mod.LLMResponse(
                content=f"answer-{self._call}",
                tool_calls=[],
                finish_reason="stop",
            )
            # Attach shim — production code sets this via _parse_response.
            try:
                resp.cache_stats = _Shim(prompt_tokens, cached_tokens)
            except Exception:
                pass
            if on_text_chunk:
                on_text_chunk(resp.content or "")
            return resp

        def chat(self, messages, tools=None, timeout=None):
            return chat_mod.LLMResponse(content="answer", tool_calls=[],
                                        finish_reason="stop")

    return _ScriptedLLM()


def test_20_turn_maintains_ratio() -> bool:
    """AC-008: mean(ratios[2..20]) >= 0.70 across 20 scripted turns."""
    print("\n=== TEST AC-008: 20-turn conversation maintains cache ratio >= 0.70 ===")

    from src.agent.loop import AgentLoop
    from src.agent.tools import ToolRegistry

    TURNS = 20
    scripted = _make_growing_cache_llm(TURNS)
    reg = ToolRegistry()

    observed_ratios: List[Optional[float]] = []

    # Capture cache_stats events if AgentLoop emits them.
    events: List[tuple] = []

    agent = AgentLoop(
        registry=reg,
        llm=scripted,
        event_callback=lambda et, data: events.append((et, data)),
        max_iterations=3,
    )

    # Drive 20 turns. Each .run() is one LLM call (no tool calls).
    for i in range(TURNS):
        agent.run(user_message=f"turn-{i + 1}", history=None,
                  session_id=f"ac008-{i}")

    # Collect ratios from cache_stats events emitted by AgentLoop.
    cache_stats_events = [d for et, d in events if et == "cache_stats"]
    for data in cache_stats_events:
        observed_ratios.append(data.get("ratio"))

    print(f"  cache_stats events emitted by AgentLoop: {len(cache_stats_events)}")
    print(f"  observed ratios (turns 2..20): {[r for r in observed_ratios[1:]]}")

    if len(cache_stats_events) < TURNS - 1:
        print(f"  FAIL: AgentLoop emitted {len(cache_stats_events)} 'cache_stats' events; "
              f"need at least {TURNS - 1} (turns 2..20) to compute the mean. Phase 2 "
              f"must emit per-turn cache_stats events with a 'ratio' field.")
        return False

    ratios_2_to_20 = [r for r in observed_ratios[1:TURNS] if r is not None]
    if len(ratios_2_to_20) < TURNS - 1:
        print(f"  FAIL: only {len(ratios_2_to_20)} non-None ratios across turns 2..20; "
              f"need all {TURNS - 1}.")
        return False

    mean_ratio = statistics.mean(ratios_2_to_20)
    print(f"  mean(ratios[2..20]) = {mean_ratio:.4f}")

    if mean_ratio < 0.70:
        print(f"  FAIL: mean ratio = {mean_ratio:.4f}, expected >= 0.70")
        return False

    assert mean_ratio >= 0.70, "20-turn long conversation maintains cache ratio"
    print(f"  OK: mean ratio = {mean_ratio:.4f} >= 0.70")
    return True


def main() -> None:
    results = [
        test_20_turn_maintains_ratio(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
