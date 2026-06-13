"""Smoke test for subagent + team module.

Tests:
  1. delegate single subagent (no web, fast)
  2. spawn_team with research_team preset (full DAG)
  3. depth limit enforcement
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))
RUNS_DIR = AGENT_DIR / "runs" / "test_subagent"


def banner(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print('=' * 70)


def test1_delegate_single() -> bool:
    banner("TEST 1: delegate() single subagent (no tools, fast LLM call)")
    from src.providers.chat import ChatLLM
    from src.agent.tools import ToolRegistry, BaseTool
    from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner

    class _NoopTool(BaseTool):
        name = "noop"
        description = "does nothing"
        parameters = {"type": "object", "properties": {}}
        repeatable = True
        is_readonly = True

        def execute(self, **kw):
            return '{"status":"ok"}'

    registry = ToolRegistry()
    registry.register(_NoopTool())

    parent_run_dir = RUNS_DIR / f"test1_{int(time.time())}"
    parent_run_dir.mkdir(parents=True, exist_ok=True)
    ctx = SubAgentContext(depth=0, parent_run_dir=parent_run_dir, parent_session_id="test1")

    cfg = SubAgentConfig(
        role="leaf",
        goal="用一句中文回答：1+1等于几？只回答数字，不要解释。",
        max_iterations=3,
        timeout_sec=60,
    )

    runner = SubAgentRunner(ChatLLM(), registry, parent_run_dir, ctx)
    t0 = time.time()
    result = runner.run(cfg)
    elapsed = time.time() - t0

    print(f"  Status:     {result.get('status')}")
    print(f"  Content:    {result.get('content', '')[:200]!r}")
    print(f"  Role:       {result.get('subagent_role')}")
    print(f"  SubRunDir:  {result.get('subagent_run_dir')}")
    print(f"  Depth:      {result.get('depth')}")
    print(f"  Elapsed:    {elapsed:.1f}s")

    sub_trace = Path(result.get("subagent_run_dir", "")) / "trace.jsonl"
    parent_trace = parent_run_dir / "trace.jsonl"
    print(f"  SubTrace:   exists={sub_trace.exists()}")
    print(f"  ParentTrace: exists={parent_trace.exists()}")

    if parent_trace.exists():
        events = [json.loads(l) for l in parent_trace.read_text(encoding="utf-8").splitlines() if l.strip()]
        sub_starts = [e for e in events if e.get("type") == "subagent_start"]
        sub_ends = [e for e in events if e.get("type") == "subagent_end"]
        print(f"  Parent trace events: {len(events)} (subagent_start={len(sub_starts)}, subagent_end={len(sub_ends)})")

    ok = result.get("status") == "success" and bool(result.get("content"))
    print(f"  RESULT:     {'PASS' if ok else 'FAIL'}")
    return ok


def test2_spawn_team() -> bool:
    banner("TEST 2: spawn_team() with research_team preset (full DAG)")
    from src.providers.chat import ChatLLM
    from src.tools import build_registry
    from src.agent.subagent import SubAgentContext
    from src.agent.team import TeamRunner
    from src.agent.presets import load_preset

    parent_run_dir = RUNS_DIR / f"test2_{int(time.time())}"
    parent_run_dir.mkdir(parents=True, exist_ok=True)

    registry = build_registry()
    ctx = SubAgentContext(depth=0, parent_run_dir=parent_run_dir, parent_session_id="test2")

    preset = load_preset("research_team")
    runner = TeamRunner(ChatLLM(), registry, parent_run_dir, ctx)
    variables = {"topic": "Rust 编程语言 2026 年的就业前景"}

    print(f"  Preset:     {preset.name}")
    print(f"  Agents:     {[a.id for a in preset.agents]} + aggregator={preset.aggregator.id if preset.aggregator else None}")
    print(f"  Topic:      {variables['topic']}")
    print(f"  (This runs 4 LLM-driven subagents — may take 1-3 min)")
    print()

    t0 = time.time()
    result = runner.run(preset, variables)
    elapsed = time.time() - t0

    print(f"\n  Status:     {result.get('status')}")
    print(f"  Reason:     {result.get('reason', '-')}")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print(f"  Agents ran:")
    for aid, res in (result.get("agents") or {}).items():
        content_preview = (res.get("content") or "")[:150].replace("\n", " ")
        print(f"    - {aid}: status={res.get('status')}, content={content_preview!r}")

    summary = result.get("content", "")
    print(f"\n  Final summary (first 400 chars):")
    print(f"    {summary[:400]!r}")

    agent_results = result.get("agents") or {}
    ok = result.get("status") == "success" and len(agent_results) >= 3
    print(f"\n  RESULT:     {'PASS' if ok else 'FAIL'}")
    return ok


def test3_depth_limit() -> bool:
    banner("TEST 3: depth limit enforcement")
    from src.providers.chat import ChatLLM
    from src.agent.tools import ToolRegistry
    from src.agent.subagent import SubAgentConfig, SubAgentContext, SubAgentRunner

    parent_run_dir = RUNS_DIR / f"test3_{int(time.time())}"
    parent_run_dir.mkdir(parents=True, exist_ok=True)

    ctx_at_max = SubAgentContext(depth=2, parent_run_dir=parent_run_dir, parent_session_id="test3")
    runner = SubAgentRunner(ChatLLM(), ToolRegistry(), parent_run_dir, ctx_at_max)

    cfg = SubAgentConfig(role="leaf", goal="should be blocked", max_iterations=2)
    result = runner.run(cfg)

    print(f"  Depth:      {ctx_at_max.depth}")
    print(f"  Status:     {result.get('status')}")
    print(f"  Reason:     {result.get('reason')}")

    ok = result.get("status") == "failed" and "max spawn depth" in (result.get("reason") or "")
    print(f"  RESULT:     {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"CWD:    {AGENT_DIR}")
    print(f"Runs:   {RUNS_DIR}")

    results = []
    results.append(("test1 delegate single", test1_delegate_single()))
    results.append(("test3 depth limit", test3_depth_limit()))
    results.append(("test2 spawn_team DAG", test2_spawn_team()))

    banner("SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
