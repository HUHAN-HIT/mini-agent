"""Tests for the rolling 3-turn cache_warning threshold (Phase 2, AC-005).

AC-005 (rolling 3-turn window of low ratios fires a warning; deque cleared
after fire; turn-4 with low ratio does NOT re-fire immediately):

    Script an AgentLoop with a fake LLM returning cache_stats.cache_hit_ratio
    = (0.3, 0.4, 0.45) on three consecutive stream_chat calls. After turn 3,
    event_callback must have received an event of type 'cache_warning' with
    payload containing 'ratios'. A 4th turn with ratio=0.2 must NOT immediately
    re-fire (deque cleared after fire).

Today, AgentLoop has no `_recent_ratios` deque, no `cache_warning` event type,
and `_emit` only forwards the existing event types. So this test fails because
no cache_warning event is ever observed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


def _make_scripted_llm(ratios: List[float]):
    """Build a scripted LLM whose stream_chat returns N turns, each with a fake
    cache_stats carrying the given ratio.

    Because `cache_stats` is the Phase-2 attribute being added to LLMResponse,
    we attach a small object shim that exposes the same attribute. Once the
    production CacheStats class lands, this shim will simply be replaced by
    setting `cache_stats=CacheStats(...)` on the real LLMResponse. The shim
    is here ONLY to give the AgentLoop something to read ratios from.
    """
    from src.providers.chat import LLMResponse

    class _Shim:
        def __init__(self, ratio: Optional[float]) -> None:
            self.ratio = ratio

        @property
        def is_available(self) -> bool:
            return self.ratio is not None

        @property
        def cache_hit_ratio(self) -> Optional[float]:
            return self.ratio

    class _ScriptedLLM:
        def __init__(self) -> None:
            self._call = 0

        def stream_chat(self, messages, tools=None, on_text_chunk=None,
                        on_reasoning_chunk=None, timeout=None):
            self._call += 1
            idx = self._call - 1
            ratio = ratios[idx] if idx < len(ratios) else None
            resp = LLMResponse(content=f"answer-{self._call}", tool_calls=[],
                               finish_reason="stop")
            # Attach shim — production code will set this via _parse_response.
            # NOTE: this bypasses the production CacheStats class, which does
            # not exist yet. The AgentLoop must read `cache_stats.cache_hit_ratio`.
            # If AgentLoop has no such reading logic, the warning never fires —
            # and that is exactly the right-reason RED failure.
            try:
                resp.cache_stats = _Shim(ratio)
            except Exception:
                pass
            if on_text_chunk:
                on_text_chunk(resp.content or "")
            return resp

        def chat(self, messages, tools=None, timeout=None):
            return LLMResponse(content="answer", tool_calls=[], finish_reason="stop")

    return _ScriptedLLM()


def test_warning_fires_on_three_low_ratios() -> bool:
    """AC-005: exactly one 'cache_warning' after turn 3; no re-fire after turn 4."""
    print("\n=== TEST AC-005: cache_warning fires on 3 consecutive low ratios ===")

    from src.agent.loop import AgentLoop
    from src.agent.tools import ToolRegistry

    # 4 scripted ratios: 0.3, 0.4, 0.45, 0.2
    # First 3 below 0.5 → should fire exactly once after turn 3.
    # Turn 4 (0.2) should NOT re-fire because the deque is cleared after fire.
    ratios = [0.3, 0.4, 0.45, 0.2]
    scripted = _make_scripted_llm(ratios)
    reg = ToolRegistry()

    events: List[tuple] = []

    agent = AgentLoop(
        registry=reg,
        llm=scripted,
        event_callback=lambda et, data: events.append((et, data)),
        max_iterations=10,
    )

    # Drive 4 separate single-turn runs (each run is one LLM call here, since
    # the scripted LLM returns no tool calls). Each call is a turn from the
    # rolling-window perspective.
    for i in range(4):
        agent.run(user_message=f"turn-{i + 1}", history=None, session_id=f"ac005-{i}")

    cache_warnings = [(et, d) for et, d in events if et == "cache_warning"]
    print(f"  events observed: {len(events)} total")
    print(f"  'cache_warning' events: {len(cache_warnings)}")
    for et, d in cache_warnings:
        print(f"    payload: {d}")

    ok = True
    if len(cache_warnings) == 0:
        print("  FAIL: no 'cache_warning' event emitted — AgentLoop has no rolling "
              "ratio deque / no cache_warning emission path. Phase 2 work needed.")
        ok = False
    elif len(cache_warnings) != 1:
        print(f"  FAIL: expected exactly 1 cache_warning (after turn 3), got "
              f"{len(cache_warnings)}; deque must be cleared after fire so turn 4 "
              f"with ratio=0.2 must NOT re-fire.")
        ok = False
    else:
        # Inspect payload
        _, payload = cache_warnings[0]
        ratios_in_payload = payload.get("ratios")
        if not isinstance(ratios_in_payload, (list, tuple)) or len(ratios_in_payload) != 3:
            print(f"  FAIL: cache_warning payload 'ratios' = {ratios_in_payload!r}, "
                  f"expected list of 3 floats")
            ok = False
        else:
            print(f"  OK: cache_warning fired once with ratios={ratios_in_payload}")

    if ok:
        print("  OK: exactly one cache_warning observed after turn 3; turn 4 did not re-fire")
    if not ok:
        return False
    assert ok, "cache_warning fires once on 3 low ratios, deque cleared"
    return True


def main() -> None:
    results = [
        test_warning_fires_on_three_low_ratios(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
