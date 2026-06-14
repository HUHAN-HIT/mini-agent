"""Tests for CLI cache hit ratio line output (Phase 2, AC-004).

AC-004 (CLI regex on each available turn):
    CLI output must contain a line matching regex
        ^\\[cache: \\d+K/\\d+K cached, \\d+%\\]$
    on each turn where cache_stats.is_available. When cache_stats.cache_hit_ratio
    is None, NO such line is printed.

This test exercises the production formatter `cli.format_cache_stats_line`
extracted to module level for testability. It also verifies end-to-end that
AgentLoop emits a `cache_stats` event with the required payload shape, then
confirms the production formatter turns that payload into a matching CLI line.
"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import List

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

_CACHE_LINE_RE = re.compile(r"^\[cache: \d+K/\d+K cached, \d+%\]$")


def test_cache_ratio_line_format() -> bool:
    """AC-004: regex matches >=1 line per available turn; zero matching lines
    when ratio is None. Pins the production formatter directly."""
    print("\n=== TEST AC-004: CLI emits [cache: NK/NK cached, NN%] per available turn ===")

    # Import the production formatter extracted from cli.py::_on_event so the
    # test exercises real production code, not a test-local mirror.
    import cli

    # Case 1: happy-path payload produces a regex-matching line.
    happy_payload = {"iter": 1, "cached": 4200, "prompt": 5119, "ratio": 0.827}
    line1 = cli.format_cache_stats_line(happy_payload)
    if not isinstance(line1, str):
        print(f"  FAIL: formatter returned {type(line1).__name__} for happy payload, expected str")
        return False
    if not _CACHE_LINE_RE.match(line1):
        print(f"  FAIL: line {line1!r} does not match regex {_CACHE_LINE_RE.pattern}")
        return False
    print(f"  OK happy: {line1!r} matches")

    # Case 2: ratio None → formatter returns None (CLI prints nothing).
    none_payload = {"iter": 2, "cached": 0, "prompt": 0, "ratio": None}
    line2 = cli.format_cache_stats_line(none_payload)
    if line2 is not None:
        print(f"  FAIL: formatter returned {line2!r} for None ratio, expected None")
        return False
    print("  OK none: formatter returns None when ratio is None (silent)")

    # Case 3: missing keys + ratio present → formatter still produces a line
    # (cached/prompt default to 0 → 0K/0K). Confirms defensive `or 0` handling.
    sparse_payload = {"ratio": 0.5}
    line3 = cli.format_cache_stats_line(sparse_payload)
    if not isinstance(line3, str) or not _CACHE_LINE_RE.match(line3):
        print(f"  FAIL: sparse payload produced {line3!r}, expected matching line")
        return False
    print(f"  OK sparse: {line3!r} matches (defensive defaults)")

    # Case 4: end-to-end via AgentLoop. Drive one turn with a scripted LLM
    # that yields cache_stats; capture the event_callback payload; run it
    # through the production formatter. This pins the wiring: AgentLoop must
    # emit `cache_stats` events with `ratio`/`cached`/`prompt` keys that the
    # production formatter consumes.
    from src.agent.loop import AgentLoop
    from src.agent.tools import ToolRegistry

    class _ScriptedLLM:
        def __init__(self) -> None:
            self._call = 0

        def stream_chat(self, messages, tools=None, on_text_chunk=None,
                        on_reasoning_chunk=None, timeout=None):
            from src.providers.chat import LLMResponse, CacheStats
            self._call += 1
            if on_text_chunk:
                on_text_chunk(f"answer-{self._call}")
            resp = LLMResponse(content=f"answer-{self._call}", tool_calls=[],
                               finish_reason="stop")
            # Mirror what production _parse_response would populate on a real
            # OpenAI/DeepSeek response with ~82% cache hit.
            resp.cache_stats = CacheStats(
                prompt_tokens=5119,
                cached_tokens=4200,
                cache_hit_ratio=4200 / 5119,
            )
            return resp

        def chat(self, messages, tools=None, timeout=None):
            from src.providers.chat import LLMResponse
            return LLMResponse(content="answer", tool_calls=[], finish_reason="stop")

    events: List[tuple] = []

    def _capture(et, data):
        events.append((et, data))

    reg = ToolRegistry()
    scripted = _ScriptedLLM()
    agent = AgentLoop(
        registry=reg,
        llm=scripted,
        event_callback=_capture,
        max_iterations=3,
    )
    result = agent.run(user_message="hi", history=None, session_id="ac004")

    cache_stats_events = [d for et, d in events if et == "cache_stats"]
    if not cache_stats_events:
        print(f"  FAIL: AgentLoop emitted 0 'cache_stats' events. "
              f"Events seen: {[et for et, _ in events]}")
        return False

    # Run each emitted payload through the production formatter; every line
    # must match the regex. This is the real AC-004 assertion: production
    # AgentLoop + production formatter → regex-matching CLI output.
    captured = io.StringIO()
    with redirect_stdout(captured):
        for payload in cache_stats_events:
            line = cli.format_cache_stats_line(payload)
            if line is not None:
                print(line)
    matching_lines = [ln for ln in captured.getvalue().splitlines()
                      if _CACHE_LINE_RE.match(ln)]

    if len(matching_lines) < 1:
        print(f"  FAIL: 0 matching lines after running {len(cache_stats_events)} "
              f"payloads through production formatter")
        return False

    print(f"  OK end-to-end: AgentLoop emitted {len(cache_stats_events)} "
          f"'cache_stats' events; production formatter produced "
          f"{len(matching_lines)} matching CLI lines")
    print(f"  agent run status: {result.get('status')}")
    assert len(matching_lines) >= 1, "AC-004 CLI cache line emitted on available turn"
    return True


def main() -> None:
    results = [
        test_cache_ratio_line_format(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
