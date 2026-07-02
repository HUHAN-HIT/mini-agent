"""mini-agent 交互 CLI 前门：装配组件 → onboarding → banner → REPL。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2]
RUNS_DIR = AGENT_DIR / "runs"


def _configure_stdio_utf8() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 以免 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def run_repl(*, agent, skills, renderer, reader, console, history) -> None:
    """REPL 主循环。斜杠命令本地拦截；其余喂给 agent，用 renderer 流式展示。"""
    from rich.text import Text

    from src.cli.commands import handle_builtin_command

    while True:
        try:
            user_input = reader.read()
        except (EOFError, KeyboardInterrupt):
            console.print(Text("\nGoodbye!"))
            break

        if not user_input:
            continue

        cmd = handle_builtin_command(user_input, history=history, skills=skills)
        if cmd.handled:
            if cmd.clear_screen:
                console.clear()
            if cmd.clear_history:
                history.clear()
            if cmd.output:
                console.print(cmd.output)
            if cmd.quit:
                break
            continue

        renderer.begin()
        # AgentLoop 的 event_callback 已在 main() 构造时固定注入 renderer.handle，
        # 因此这里只传 user_message / history。
        result = agent.run(user_message=user_input, history=history or None)

        status = result.get("status", "unknown")
        content = result.get("content", "")
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})
        renderer.finish(status=status, content=content, run_dir=result.get("run_dir"))


def main() -> None:
    _configure_stdio_utf8()
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))

    from src.cli.banner import print_banner
    from src.cli.input import InteractiveInput
    from src.cli.onboard import needs_onboarding, run_onboarding
    from src.cli.stream import StreamRenderer
    from src.cli.theme import get_console

    console = get_console()

    if needs_onboarding():
        run_onboarding(console)

    from src.agent.loop import AgentLoop
    from src.agent.skills import SkillsLoader
    from src.agent.subagent import SubAgentContext
    from src.memory.persistent import PersistentMemory
    from src.providers.chat import ChatLLM
    from src.tools import build_registry
    from src.tools.delegate_tool import DelegateTool
    from src.tools.team_tool import TeamTool

    pm = PersistentMemory()
    llm = ChatLLM()
    registry = build_registry(persistent_memory=pm)
    skills = list(SkillsLoader().skills)

    parent_ctx = SubAgentContext(depth=0, parent_run_dir=RUNS_DIR, parent_session_id="cli")
    registry.register(DelegateTool(llm, registry, RUNS_DIR, parent_ctx))
    registry.register(TeamTool(llm, registry, RUNS_DIR, parent_ctx))

    renderer = StreamRenderer(console)
    agent = AgentLoop(
        registry=registry, llm=llm, max_iterations=50,
        persistent_memory=pm,
        event_callback=lambda et, data: renderer.handle(et, data),
    )

    provider = os.getenv("LANGCHAIN_PROVIDER", "default")
    model = llm.model_name or os.getenv("LANGCHAIN_MODEL_NAME", "default")

    version = _read_version()

    reader = InteractiveInput(provider=provider, model=model, skills=len(skills))
    if reader.is_tty:
        print_banner(console, provider=provider, model=model,
                     skills=len(skills), version=version)

    history: list[dict] = []
    run_repl(agent=agent, skills=skills, renderer=renderer,
             reader=reader, console=console, history=history)


def _read_version() -> str:
    try:
        from importlib.metadata import version as _v

        return _v("mini-agent")
    except Exception:  # noqa: BLE001
        return "0.1.0"


__all__ = ["main", "run_repl"]
