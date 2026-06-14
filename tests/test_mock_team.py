"""Mock LLM test for subagent + team — no real API calls.

Uses a deterministic stub LLM that returns scripted responses, so we can
verify the full plumbing: registry filter, depth tracking, DAG scheduling,
upstream injection, trace writes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))
RUNS_DIR = AGENT_DIR / "runs" / "test_mock"


class MockLLMResponse:
    def __init__(self, content: str, tool_calls: list = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None
        self.finish_reason = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class MockLLM:
    """Deterministic LLM stub. Returns canned content based on call count."""

    def __init__(self, scripted: List[str], model_name: str = "mock-1") -> None:
        self._scripted = scripted
        self._idx = 0
        self.model_name = model_name
        self.calls: List[List[Dict]] = []

    def chat(self, messages, tools=None, timeout=None) -> MockLLMResponse:
        self.calls.append(messages)
        idx = min(self._idx, len(self._scripted) - 1)
        self._idx += 1
        return MockLLMResponse(content=self._scripted[idx])

    def stream_chat(self, messages, tools=None, on_text_chunk=None, on_reasoning_chunk=None, timeout=None) -> MockLLMResponse:
        resp = self.chat(messages, tools=tools, timeout=timeout)
        if on_text_chunk and resp.content:
            on_text_chunk(resp.content)
        if on_reasoning_chunk and resp.reasoning_content:
            on_reasoning_chunk(resp.reasoning_content)
        return resp

    async def achat(self, messages, tools=None, timeout=None) -> MockLLMResponse:
        return self.chat(messages, tools=tools, timeout=timeout)


def banner(t: str) -> None:
    print(f"\n{'=' * 70}\n  {t}\n{'=' * 70}")


def test_delegate_basic() -> bool:
    banner("TEST 1: delegate() basic — subagent returns scripted answer")
    from src.agent.tools import ToolRegistry, BaseTool
    from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner

    class _Noop(BaseTool):
        name = "noop"
        description = "noop"
        parameters = {"type": "object", "properties": {}}
        repeatable = True
        is_readonly = True

        def execute(self, **kw):
            return '{"status":"ok"}'

    run_dir = RUNS_DIR / f"t1_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry()
    reg.register(_Noop())

    llm = MockLLM(scripted=["42 是答案"])
    ctx = SubAgentContext(depth=0, parent_run_dir=run_dir, parent_session_id="t1")
    runner = SubAgentRunner(llm, reg, run_dir, ctx)

    cfg = SubAgentConfig(role="leaf", goal="what is the answer", max_iterations=3, timeout_sec=30)
    result = runner.run(cfg)

    print(f"  Status:    {result['status']}")
    print(f"  Content:   {result.get('content')!r}")
    print(f"  Role:      {result.get('subagent_role')}")
    print(f"  Depth:     {result.get('depth')}")
    print(f"  SubRunDir: {result.get('subagent_run_dir')}")

    sub_trace = Path(result["subagent_run_dir"]) / "trace.jsonl"
    parent_trace = run_dir / "trace.jsonl"
    assert sub_trace.exists(), "sub trace missing"
    assert parent_trace.exists(), "parent trace missing"

    events = [json.loads(l) for l in parent_trace.read_text(encoding="utf-8").splitlines() if l.strip()]
    starts = [e for e in events if e.get("type") == "subagent_start"]
    ends = [e for e in events if e.get("type") == "subagent_end"]
    print(f"  Parent trace: {len(events)} events, {len(starts)} starts, {len(ends)} ends")

    ok = (
        result["status"] == "success"
        and result.get("content") == "42 是答案"
        and result.get("depth") == 1
        and len(starts) == 1
        and len(ends) == 1
        and ends[0].get("status") == "success"
    )
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def test_depth_limit() -> bool:
    banner("TEST 2: depth limit — depth=2 blocked")
    from src.agent.tools import ToolRegistry
    from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner

    run_dir = RUNS_DIR / f"t2_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = SubAgentContext(depth=2, parent_run_dir=run_dir)
    runner = SubAgentRunner(MockLLM(["x"]), ToolRegistry(), run_dir, ctx)
    result = runner.run(SubAgentConfig(goal="should fail", max_iterations=2))

    print(f"  Status: {result['status']}, reason: {result.get('reason')}")
    ok = result["status"] == "failed" and "max spawn depth" in result.get("reason", "")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def test_team_dag() -> bool:
    banner("TEST 3: spawn_team DAG — 2 parallel + 1 merge + aggregator")
    from src.agent.team import AgentSpec, TeamPreset, TeamRunner
    from src.agent.tools import ToolRegistry
    from src.agent.subagent import SubAgentContext

    run_dir = RUNS_DIR / f"t3_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)

    def make_agent(aid: str, **kw) -> AgentSpec:
        return AgentSpec(id=aid, role="leaf", goal=f"task for {aid}", **kw)

    preset = TeamPreset(
        name="test_dag",
        description="3-node DAG",
        agents=[
            make_agent("a"),
            make_agent("b"),
            make_agent("c", depends_on=["a", "b"], input_from=["a", "b"]),
        ],
        aggregator=AgentSpec(id="agg", role="specialist", goal="merge all", input_from=["a", "b", "c"]),
    )

    call_count = {"n": 0}

    class CountingLLM(MockLLM):
        def chat(self, messages, tools=None, timeout=None):
            call_count["n"] += 1
            last_user = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    last_user = str(m.get("content", ""))
                    break
            has_upstream = "<upstream-results>" in last_user
            tag = "MERGE" if has_upstream else "LEAF"
            return MockLLMResponse(content=f"[{tag}] call#{call_count['n']} saw: {last_user[:80]!r}")

    llm = CountingLLM(scripted=[])
    ctx = SubAgentContext(depth=0, parent_run_dir=run_dir, parent_session_id="t3")
    runner = TeamRunner(llm, ToolRegistry(), run_dir, ctx)

    t0 = time.time()
    result = runner.run(preset, {})
    elapsed = time.time() - t0

    print(f"  Status:    {result['status']}")
    print(f"  Elapsed:   {elapsed:.2f}s")
    print(f"  LLM calls: {call_count['n']}")
    print(f"  Agents ran: {list((result.get('agents') or {}).keys())}")

    for aid, res in (result.get("agents") or {}).items():
        c = (res.get("content") or "")[:120]
        print(f"    - {aid}: {c!r}")

    final = (result.get("content") or "")[:200]
    print(f"  Final content: {final!r}")

    ok = (
        result["status"] == "success"
        and call_count["n"] == 4
        and set((result.get("agents") or {}).keys()) == {"a", "b", "c"}
        and "MERGE" in result.get("content", "")
    )
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def test_team_upstream_injection() -> bool:
    banner("TEST 4: upstream_context injection — verifier checks downstream sees upstream output")
    from src.agent.team import AgentSpec, TeamPreset, TeamRunner
    from src.agent.tools import ToolRegistry
    from src.agent.subagent import SubAgentContext

    run_dir = RUNS_DIR / f"t4_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)

    captured: List[str] = []

    class CaptureLLM(MockLLM):
        def chat(self, messages, tools=None, timeout=None):
            for m in messages:
                if m.get("role") == "user":
                    captured.append(str(m.get("content", "")))
            return MockLLMResponse(content=f"output from agent")

    preset = TeamPreset(
        name="upstream_test",
        agents=[
            AgentSpec(id="producer", role="leaf", goal="produce data"),
            AgentSpec(id="consumer", role="leaf", goal="consume data",
                      depends_on=["producer"], input_from=["producer"]),
        ],
    )

    runner = TeamRunner(CaptureLLM(scripted=[]), ToolRegistry(), run_dir,
                       SubAgentContext(depth=0, parent_run_dir=run_dir))
    runner.run(preset, {})

    producer_msgs = [m for m in captured if "produce data" in m and "<upstream-results>" not in m]
    consumer_msgs = [m for m in captured if "<upstream-results>" in m]

    print(f"  Producer messages: {len(producer_msgs)}")
    print(f"  Consumer messages with <upstream-results>: {len(consumer_msgs)}")
    if consumer_msgs:
        sample = consumer_msgs[0][:300]
        print(f"  Sample consumer msg: {sample!r}")

    ok = len(consumer_msgs) >= 1 and "output from agent" in consumer_msgs[0]
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def test_cycle_detection_at_load() -> bool:
    banner("TEST 5: cycle detection — bad YAML fails fast at load")
    from src.agent.presets.loader import _load_yaml, _PRESETS_DIR
    from src.agent.team import TeamPreset, TeamRunner
    import tempfile, os

    bad_yaml = """
name: cycle
agents:
  - id: x
    depends_on: [y]
    goal: x
  - id: y
    depends_on: [x]
    goal: y
"""
    tmp = Path(tempfile.gettempdir()) / f"bad_preset_{int(time.time())}.yaml"
    tmp.write_text(bad_yaml, encoding="utf-8")
    try:
        raw = _load_yaml(tmp)
        preset = TeamPreset.from_dict(raw)
        try:
            TeamRunner._topo_sort(preset.agents)
            print(f"  Cycle NOT detected — FAIL")
            return False
        except ValueError as e:
            print(f"  Detected: {e}")
            print(f"  RESULT: PASS")
            return True
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"CWD:    {AGENT_DIR}")
    print(f"Runs:   {RUNS_DIR}")

    tests = [
        ("delegate basic", test_delegate_basic),
        ("depth limit", test_depth_limit),
        ("team DAG (3+1)", test_team_dag),
        ("upstream injection", test_team_upstream_injection),
        ("cycle detection", test_cycle_detection_at_load),
    ]
    results = [(name, fn()) for name, fn in tests]

    banner("SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
