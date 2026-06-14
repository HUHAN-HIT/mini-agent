"""Tests for the silent_compress trace event gating (Phase 6, AC-006/AC-007).

AC-006 (no silent_compress below threshold):
    When raw estimated tokens < TOKEN_THRESHOLD * 0.85, no silent_compress
    trace event is emitted (Layer 1 microcompact and Layer 2 collapse do not fire).

AC-007 (silent_compress fires above threshold):
    When raw estimated tokens > TOKEN_THRESHOLD * 0.85 but < TOKEN_THRESHOLD,
    Layer 1 (microcompact) and/or Layer 2 (collapse) fire and each writes a
    silent_compress trace event with a valid layer value before mutating messages.

Today:
  - `_microcompact` (loop.py:53) is called UNCONDITIONALLY at line 291, every
    iteration, with NO trace event. It silently mutates messages.
  - `_context_collapse` (loop.py:63) IS gated by COLLAPSE_THRESHOLD at line 294,
    but emits no trace event.
  - There is no 'silent_compress' trace type anywhere.

So AC-006 fails (microcompact fires below threshold today, and the test cannot
observe a "did not fire" without a trace signal, so we instead verify the
trace contains zero silent_compress entries — which currently passes trivially
because the event type does not exist; that's a vacuous pass. The real test of
the gating behavior is that NO microcompact mutation happens below threshold,
which we observe via the user-visible state of messages).

To make AC-006 fail for the right reason, we additionally assert that messages
below threshold are NOT mutated by _microcompact. Today they ARE (because
_microcompact is unconditional), so this is the load-bearing assertion.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


def _make_scripted_llm():
    """LLM that just answers without tool calls."""
    from src.providers.chat import LLMResponse

    class _ScriptedLLM:
        def stream_chat(self, messages, tools=None, on_text_chunk=None,
                        on_reasoning_chunk=None, timeout=None):
            if on_text_chunk:
                on_text_chunk("answer")
            return LLMResponse(content="answer", tool_calls=[], finish_reason="stop")

        def chat(self, messages, tools=None, timeout=None):
            return LLMResponse(content="answer", tool_calls=[], finish_reason="stop")

    return _ScriptedLLM()


def _build_agent_with_run_dir(run_dir: Path):
    from src.agent.loop import AgentLoop
    from src.agent.memory import WorkspaceMemory
    from src.agent.tools import ToolRegistry

    reg = ToolRegistry()
    memory = WorkspaceMemory(run_dir=str(run_dir))
    scripted = _make_scripted_llm()
    agent = AgentLoop(
        registry=reg,
        llm=scripted,
        memory=memory,
        max_iterations=3,
    )
    return agent


def test_no_silent_compress_below_threshold() -> bool:
    """AC-006: below 0.85 * TOKEN_THRESHOLD, no silent_compress event."""
    print("\n=== TEST AC-006: no silent_compress below threshold ===")

    from src.agent.loop import TOKEN_THRESHOLD
    from src.agent.trace import TraceWriter

    threshold_85 = int(TOKEN_THRESHOLD * 0.85)
    print(f"  TOKEN_THRESHOLD = {TOKEN_THRESHOLD}, 0.85 * T = {threshold_85}")

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        agent = _build_agent_with_run_dir(run_dir)

        # Build a small message set well below threshold.
        # We pre-seed history with a few tool messages that would normally
        # be candidates for _microcompact mutation.
        from src.agent.context import ContextBuilder
        from src.agent.memory import WorkspaceMemory
        from src.agent.tools import ToolRegistry

        # Drive the agent with one small user message.
        # The agent should not emit any silent_compress trace entry.
        result = agent.run(user_message="hi", history=None, session_id="ac006")

        trace_entries = TraceWriter.read(run_dir)
        silent_compress = [e for e in trace_entries if e.get("type") == "silent_compress"]

        print(f"  run status: {result.get('status')}")
        print(f"  trace entries: {len(trace_entries)} total")
        print(f"  silent_compress entries: {len(silent_compress)}")

        # Today: silent_compress event type does not exist → 0 entries → vacuously passes.
        # We additionally assert that the OLD `_microcompact` path is NOT triggered
        # below threshold. Today `_microcompact` runs unconditionally (loop.py:291),
        # so this assertion fails until the gate is added.
        #
        # To detect microcompact mutation without the trace event, we observe that
        # in this scenario (no tool calls), the message list is tiny and unchanged.
        # The observable contract we assert is: silent_compress count == 0.
        # The "right reason" failure for AC-006 occurs when microcompact fires
        # unconditionally AND emits a trace event (Phase 6 implementation); at
        # that point the count would be > 0 and the test would fail.
        #
        # For the RED phase, the test passes vacuously because no trace event
        # type exists yet. The IMPLEMENTED phase must add the event + gate so
        # that this assertion still holds (count == 0) — i.e. microcompact is
        # gated OUT below threshold.
        if len(silent_compress) != 0:
            print(f"  FAIL: expected 0 silent_compress entries below threshold, "
                  f"got {len(silent_compress)}. Entries: {silent_compress}")
            return False

        print("  OK (vacuous in RED): 0 silent_compress entries; this assertion "
              "will only become meaningful when the trace event exists + gating "
              "is added in Phase 6.")
        # NOTE: For this test to be a non-vacuous RED failure, we additionally
        # probe the microcompact gating by constructing a scenario where the
        # OLD unconditional microcompact would mutate messages below threshold.
        # See below.
        probe_ok = _probe_microcompact_not_firing_below_threshold()
        if not probe_ok:
            return False
        assert probe_ok, "no silent_compress below threshold"
        return True


def _probe_microcompact_not_firing_below_threshold() -> bool:
    """Sub-check: directly exercise the (post-Phase-6) gating logic. Pre-Phase-6,
    `_microcompact` mutates messages unconditionally when there are >KEEP_RECENT
    tool messages. The Phase-6 change wraps the call in a `tokens > MICROCOMPACT_THRESHOLD`
    gate. We assert that after the change, a small message list with >KEEP_RECENT
    tool messages is NOT mutated.

    Today (RED): _microcompact runs unconditionally, so the tool messages ARE
    cleared → assertion fails for the right reason.
    """
    print("\n  --- Sub-probe: microcompact gating on small message list ---")
    from src.agent.loop import _microcompact, KEEP_RECENT, estimate_tokens

    # Build 10 tool messages (well above KEEP_RECENT=3 today, or 6 after migration).
    messages: List[Dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for i in range(10):
        messages.append({
            "role": "tool",
            "tool_call_id": f"tc-{i}",
            "name": "noop",
            "content": "x" * 500,  # ~125 tokens each; total ~1250 tokens << threshold
        })

    pre_tokens = estimate_tokens(messages)
    print(f"  pre-_microcompact tokens: {pre_tokens}")
    print(f"  KEEP_RECENT (current): {KEEP_RECENT}")

    # Snapshot content of tool messages before mutation.
    pre_contents = [m.get("content", "") for m in messages if m.get("role") == "tool"]

    _microcompact(messages)

    post_contents = [m.get("content", "") for m in messages if m.get("role") == "tool"]
    cleared = sum(1 for pre, post in zip(pre_contents, post_contents) if pre != post)

    print(f"  tool messages cleared by _microcompact: {cleared}")

    # AC-006 contract: when raw estimated tokens < 0.85*T, microcompact does not fire.
    # Today microcompact fires unconditionally whenever len(tool_msgs) > KEEP_RECENT,
    # regardless of token count. So `cleared` should be > 0 today → fail.
    if cleared != 0:
        print(f"  FAIL: _microcompact cleared {cleared} tool messages even though "
              f"tokens ({pre_tokens}) << 0.85*T. Phase 6 must add a token gate.")
        return False

    print("  OK: _microcompact did not mutate below threshold")
    return True


def test_silent_compress_fires_above_threshold() -> bool:
    """AC-007: above 0.85 * TOKEN_THRESHOLD, silent_compress fires with valid
    layer and int tokens."""
    print("\n=== TEST AC-007: silent_compress fires above threshold ===")

    from src.agent.loop import TOKEN_THRESHOLD
    from src.agent.trace import TraceWriter

    threshold_85 = int(TOKEN_THRESHOLD * 0.85)
    print(f"  TOKEN_THRESHOLD = {TOKEN_THRESHOLD}, 0.85 * T = {threshold_85}")

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        agent = _build_agent_with_run_dir(run_dir)

        # Build a giant history whose estimated tokens exceeds 0.85*T but stays
        # below T. estimate_tokens uses len(json)/4, so we need roughly
        # 0.85 * 40000 * 4 ≈ 136000 chars of content.
        big_text = "x" * 200_000  # ~50K tokens; comfortably above 0.85*T=34K
        history = [
            {"role": "user", "content": big_text},
            {"role": "assistant", "content": big_text},
        ]

        result = agent.run(user_message="hi", history=history, session_id="ac007")

        trace_entries = TraceWriter.read(run_dir)
        silent_compress = [e for e in trace_entries if e.get("type") == "silent_compress"]

        print(f"  run status: {result.get('status')}")
        print(f"  silent_compress entries: {len(silent_compress)}")

        if len(silent_compress) == 0:
            print("  FAIL: 0 silent_compress entries above threshold — Phase 6 must "
                  "emit {'type': 'silent_compress', 'layer': ..., 'tokens': N} before "
                  "each Layer 1 / Layer 2 mutation.")
            return False

        ok = True
        for entry in silent_compress:
            layer = entry.get("layer")
            tokens = entry.get("tokens")
            if layer not in ("microcompact", "collapse"):
                print(f"  FAIL: entry layer={layer!r}, expected 'microcompact' or 'collapse'")
                ok = False
                continue
            if not isinstance(tokens, int) or isinstance(tokens, bool):
                print(f"  FAIL: entry tokens={tokens!r}, expected int (not bool)")
                ok = False
                continue
            print(f"  OK: layer={layer!r}, tokens={tokens} (int)")

        if not ok:
            return False
        assert ok, "silent_compress fires above threshold with valid layer/tokens"
        return True


def main() -> None:
    results = [
        test_no_silent_compress_below_threshold(),
        test_silent_compress_fires_above_threshold(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
