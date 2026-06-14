"""Tests for streaming text + reasoning callbacks.

Verifies:
  1. ChatLLM.stream_chat correctly routes reasoning_content to on_reasoning_chunk
     and chunk.content to on_text_chunk.
  2. AgentLoop emits thinking_delta / text_delta / thinking_done events
     in response to the streaming callbacks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))


class FakeChunk:
    """Mimics a langchain stream chunk: has .content, .additional_kwargs, .tool_calls."""

    def __init__(
        self,
        content: str = "",
        reasoning: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.content = content
        self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}
        self.tool_calls = tool_calls or []
        self.response_metadata: Dict[str, Any] = {}

    def __add__(self, other: "FakeChunk") -> "FakeChunk":
        return FakeChunk(
            content=self.content + other.content,
            reasoning=(self.additional_kwargs.get("reasoning_content") or "")
            + (other.additional_kwargs.get("reasoning_content") or ""),
            tool_calls=self.tool_calls + other.tool_calls,
        )


class FakeBoundLLM:
    """Fake of langchain's bound LLM: streams scripted chunks."""

    def __init__(self, chunks: List[FakeChunk]) -> None:
        self._chunks = chunks

    def stream(self, messages, config=None):
        return iter(self._chunks)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        accumulated = None
        for c in self._chunks:
            accumulated = c if accumulated is None else accumulated + c
        return accumulated


class FakeLLMModule:
    """Stands in for the real ChatOpenAI module path used by build_llm()."""

    def __init__(self, chunks: List[FakeChunk]) -> None:
        self._chunks = chunks


def _build_chat_llm_with_chunks(chunks: List[FakeChunk]):
    """Construct a ChatLLM whose underlying _llm streams the given chunks."""
    from src.providers import chat as chat_mod

    fake_llm = FakeBoundLLM(chunks)
    llm = chat_mod.ChatLLM.__new__(chat_mod.ChatLLM)
    llm.model_name = "fake-1"
    llm._llm = fake_llm
    return llm


def test_stream_chat_routes_reasoning_and_text() -> bool:
    print("\n=== TEST 1: stream_chat routes reasoning and text to separate callbacks ===")
    chunks = [
        FakeChunk(reasoning="Hello, "),
        FakeChunk(reasoning="world!"),
        FakeChunk(content="Answer: "),
        FakeChunk(content="42"),
    ]

    llm = _build_chat_llm_with_chunks(chunks)

    text_deltas: List[str] = []
    reasoning_deltas: List[str] = []

    resp = llm.stream_chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_chunk=text_deltas.append,
        on_reasoning_chunk=reasoning_deltas.append,
    )

    ok = True
    expected_reasoning = "Hello, world!"
    expected_text = "Answer: 42"

    if "".join(reasoning_deltas) != expected_reasoning:
        print(f"  FAIL: reasoning deltas = {''.join(reasoning_deltas)!r}, expected {expected_reasoning!r}")
        ok = False
    else:
        print(f"  OK: reasoning deltas concatenated to {expected_reasoning!r}")

    if "".join(text_deltas) != expected_text:
        print(f"  FAIL: text deltas = {''.join(text_deltas)!r}, expected {expected_text!r}")
        ok = False
    else:
        print(f"  OK: text deltas concatenated to {expected_text!r}")

    if resp.reasoning_content != expected_reasoning:
        print(f"  FAIL: resp.reasoning_content = {resp.reasoning_content!r}, expected {expected_reasoning!r}")
        ok = False
    else:
        print(f"  OK: resp.reasoning_content = {expected_reasoning!r}")

    if resp.content != expected_text:
        print(f"  FAIL: resp.content = {resp.content!r}, expected {expected_text!r}")
        ok = False
    else:
        print(f"  OK: resp.content = {expected_text!r}")

    return ok


def test_stream_chat_without_reasoning_callback() -> bool:
    print("\n=== TEST 2: stream_chat works without on_reasoning_chunk (backward compat) ===")
    chunks = [FakeChunk(content="hello"), FakeChunk(content=" world")]
    llm = _build_chat_llm_with_chunks(chunks)

    text_deltas: List[str] = []
    resp = llm.stream_chat(
        messages=[{"role": "user", "content": "hi"}],
        on_text_chunk=text_deltas.append,
    )

    ok = True
    if "".join(text_deltas) != "hello world":
        print(f"  FAIL: text deltas = {''.join(text_deltas)!r}")
        ok = False
    else:
        print("  OK: text streaming still works without reasoning callback")
    return ok


def test_agent_loop_emits_thinking_and_text_events() -> bool:
    print("\n=== TEST 3: AgentLoop emits thinking_delta / text_delta / thinking_done events ===")
    from src.agent.loop import AgentLoop
    from src.agent.tools import ToolRegistry

    class _ScriptedLLM:
        def __init__(self) -> None:
            self._call = 0

        def stream_chat(self, messages, tools=None, on_text_chunk=None, on_reasoning_chunk=None, timeout=None):
            from src.providers.chat import LLMResponse, ToolCallRequest
            self._call += 1
            if on_reasoning_chunk:
                on_reasoning_chunk("Let me think... ")
                on_reasoning_chunk("Done thinking.")
            if on_text_chunk:
                on_text_chunk("Final ")
                on_text_chunk("answer.")
            return LLMResponse(content="Final answer.", tool_calls=[], finish_reason="stop")

        def chat(self, messages, tools=None, timeout=None):
            from src.providers.chat import LLMResponse
            return LLMResponse(content="", tool_calls=[], finish_reason="stop")

    reg = ToolRegistry()
    scripted = _ScriptedLLM()
    events: List[tuple] = []

    agent = AgentLoop(
        registry=reg,
        llm=scripted,
        event_callback=lambda et, data: events.append((et, data)),
        max_iterations=3,
    )

    result = agent.run(user_message="hi", history=None, session_id="test")

    ok = True
    event_types = [e[0] for e in events]

    if "thinking_delta" not in event_types:
        print(f"  FAIL: no thinking_delta event. Events: {event_types}")
        ok = False
    else:
        thinking_chunks = [d["delta"] for et, d in events if et == "thinking_delta"]
        print(f"  OK: thinking_delta events emitted: {''.join(thinking_chunks)!r}")

    if "thinking_done" not in event_types:
        print(f"  FAIL: no thinking_done event. Events: {event_types}")
        ok = False
    else:
        print("  OK: thinking_done event emitted")

    if "text_delta" not in event_types:
        print(f"  FAIL: no text_delta event. Events: {event_types}")
        ok = False
    else:
        text_chunks = [d["delta"] for et, d in events if et == "text_delta"]
        print(f"  OK: text_delta events emitted: {''.join(text_chunks)!r}")

    if result.get("status") != "success":
        print(f"  FAIL: result status = {result.get('status')}, expected success")
        ok = False
    else:
        print(f"  OK: agent run completed with status=success")

    return ok


def main() -> None:
    results = [
        test_stream_chat_routes_reasoning_and_text(),
        test_stream_chat_without_reasoning_callback(),
        test_agent_loop_emits_thinking_and_text_events(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'=' * 60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
