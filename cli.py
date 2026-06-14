#!/usr/bin/env python3
"""Interactive CLI for Mini-Agent — a minimal ReAct agent with tools, skills, and memory."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows console 默认 cp936/GBK，emoji 和部分 Unicode 会触发 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

RUNS_DIR = AGENT_DIR / "runs"


def main() -> None:
    from src.agent.loop import AgentLoop
    from src.agent.skills import SkillsLoader
    from src.agent.subagent import SubAgentContext
    from src.providers.chat import ChatLLM
    from src.memory.persistent import PersistentMemory
    from src.tools import build_registry
    from src.tools.delegate_tool import DelegateTool
    from src.tools.team_tool import TeamTool

    print("Mini-Agent CLI")
    print("Type your message and press Enter. Type /quit to exit.\n")

    pm = PersistentMemory()
    llm = ChatLLM()
    registry = build_registry(persistent_memory=pm)
    skills_loader = SkillsLoader()

    parent_ctx = SubAgentContext(depth=0, parent_run_dir=RUNS_DIR, parent_session_id="cli")
    registry.register(DelegateTool(llm, registry, RUNS_DIR, parent_ctx))
    registry.register(TeamTool(llm, registry, RUNS_DIR, parent_ctx))

    stream_state = {"in_thinking": False, "in_text": False, "streamed_text": False}

    def _on_event(event_type: str, data: dict) -> None:
        if event_type == "thinking_delta":
            if not stream_state["in_thinking"]:
                print("\n[think] ", end="", flush=True)
                stream_state["in_thinking"] = True
                stream_state["in_text"] = False
            print(data.get("delta", ""), end="", flush=True)
        elif event_type == "text_delta":
            stream_state["streamed_text"] = True
            if not stream_state["in_text"]:
                if stream_state["in_thinking"]:
                    print()  # 换行结束 thinking 段
                print("\n[answer] ", end="", flush=True)
                stream_state["in_text"] = True
                stream_state["in_thinking"] = False
            print(data.get("delta", ""), end="", flush=True)
        elif event_type == "thinking_done":
            if stream_state["in_thinking"]:
                print()  # 换行结束 thinking 段
                stream_state["in_thinking"] = False
        elif event_type == "tool_call":
            if stream_state["in_text"] or stream_state["in_thinking"]:
                print()
                stream_state["in_text"] = False
                stream_state["in_thinking"] = False
            tool = data.get("tool", "?")
            keys = list(data.get("arguments", {}).keys())
            print(f"[tool] {tool}({', '.join(keys)})", flush=True)
        elif event_type == "tool_result":
            tool = data.get("tool", "?")
            status = data.get("status", "?")
            elapsed = data.get("elapsed_ms", 0)
            preview = data.get("preview", "")
            if preview:
                preview = " " + preview[:120].replace("\n", " ")
            print(f"[result] {tool}: {status} ({elapsed}ms){preview}", flush=True)
        elif event_type == "compact":
            tokens = data.get("tokens_before", "?")
            print(f"[compact] triggered at {tokens} tokens", flush=True)

    agent = AgentLoop(
        registry=registry,
        llm=llm,
        max_iterations=50,
        persistent_memory=pm,
        event_callback=_on_event,
    )

    history: list[dict] = []

    while True:
        try:
            user_input = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/quit", "/exit", "/q"):
            print("Goodbye!")
            break
        if user_input.lower() == "/skills":
            for s in skills_loader.skills:
                print(f"  {s.name}: {s.description}")
            continue
        if user_input.lower() == "/help":
            print("  /skills  - List available skills")
            print("  /quit    - Exit")
            print("  /help    - Show this help")
            continue

        stream_state = {"in_thinking": False, "in_text": False, "streamed_text": False}
        result = agent.run(user_message=user_input, history=history or None)
        status = result.get("status", "unknown")
        content = result.get("content", "")

        if stream_state["in_text"] or stream_state["in_thinking"]:
            print()
            stream_state["in_text"] = False
            stream_state["in_thinking"] = False

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})

        print(f"\nAgent [{status}]:")
        if not stream_state["streamed_text"] and content:
            # 没流式打印过（如 stream_chat 回退到 chat），兜底输出
            print(content)
        print()

        if run_dir := result.get("run_dir"):
            print(f"Run dir: {run_dir}\n")


if __name__ == "__main__":
    main()
