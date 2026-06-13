#!/usr/bin/env python3
"""Interactive CLI for Mini-Agent — a minimal ReAct agent with tools, skills, and memory."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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

    agent = AgentLoop(
        registry=registry,
        llm=llm,
        max_iterations=50,
        persistent_memory=pm,
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

        result = agent.run(user_message=user_input, history=history or None)
        status = result.get("status", "unknown")
        content = result.get("content", "")

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})

        print(f"\nAgent [{status}]:")
        print(content)
        print()

        if run_dir := result.get("run_dir"):
            print(f"Run dir: {run_dir}\n")


if __name__ == "__main__":
    main()
