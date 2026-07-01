#!/usr/bin/env python3
"""Interactive CLI for Mini-Agent — a minimal ReAct agent with tools, skills, and memory."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
DEFAULT_WIDTH = 88


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    output: str = ""
    clear_history: bool = False
    clear_screen: bool = False


def _terminal_width(default: int = DEFAULT_WIDTH) -> int:
    return max(64, min(shutil.get_terminal_size((default, 24)).columns, 120))


def _rule(width: int, char: str = "-") -> str:
    return char * max(10, width)


def _short_path(path: Path | str, max_len: int = 58) -> str:
    text = str(path)
    if len(text) <= max_len:
        return text
    return "..." + text[-(max_len - 3):]


def _truncate(text: str, max_len: int = 96) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def format_banner(
    *,
    provider: str,
    model: str,
    cwd: Path | str,
    skills_count: int,
    width: int | None = None,
) -> str:
    """Render the lightweight Claude Code style startup banner."""

    width = width or _terminal_width()
    lines = [
        _rule(width, "="),
        "Mini-Agent".ljust(width),
        _rule(width),
        f"provider: {provider or 'default'}    model: {model or 'default'}    skills: {skills_count}",
        f"cwd: {_short_path(cwd)}",
        "Type /help for commands. Press Ctrl+C or use /quit to exit.",
        _rule(width, "="),
    ]
    return "\n".join(lines)


def format_help() -> str:
    commands = [
        ("/help", "Show this command list."),
        ("/clear", "Clear the screen and reset in-memory CLI history."),
        ("/history", "Show recent conversation turns in this CLI session."),
        ("/skills", "List available skills."),
        ("/quit", "Exit the CLI. Aliases: /exit, /q."),
    ]
    width = _terminal_width()
    lines = [_rule(width), "Commands", _rule(width)]
    for name, desc in commands:
        lines.append(f"  {name:<10} {desc}")
    return "\n".join(lines)


def format_history_summary(history: list[dict[str, Any]], *, limit: int = 8) -> str:
    if not history:
        return "No conversation history yet."

    recent = history[-limit:]
    width = _terminal_width()
    lines = [_rule(width), f"Recent History ({len(recent)}/{len(history)} messages)", _rule(width)]
    for idx, msg in enumerate(recent, start=max(1, len(history) - len(recent) + 1)):
        role = str(msg.get("role") or "?")
        content = _truncate(str(msg.get("content") or ""), 110)
        lines.append(f"{idx:>3}. {role:<9} {content}")
    return "\n".join(lines)


def format_skills_summary(skills: list[Any], *, limit: int = 80) -> str:
    if not skills:
        return "No skills loaded."

    width = _terminal_width()
    lines = [_rule(width), f"Skills ({len(skills)})", _rule(width)]
    for skill in skills[:limit]:
        name = getattr(skill, "name", "?")
        desc = getattr(skill, "description", "")
        lines.append(f"  {name:<24} {_truncate(desc, 86)}")
    if len(skills) > limit:
        lines.append(f"  ... {len(skills) - limit} more")
    return "\n".join(lines)


def handle_builtin_command(command: str, *, history: list[dict[str, Any]], skills: list[Any]) -> CommandResult:
    normalized = command.strip().lower()
    if normalized in {"/quit", "/exit", "/q"}:
        return CommandResult(handled=True, output="Goodbye.")
    if normalized == "/help":
        return CommandResult(handled=True, output=format_help())
    if normalized == "/clear":
        return CommandResult(
            handled=True,
            output="Screen cleared. In-memory CLI history reset by /clear.",
            clear_history=True,
            clear_screen=True,
        )
    if normalized == "/history":
        return CommandResult(handled=True, output=format_history_summary(history))
    if normalized == "/skills":
        return CommandResult(handled=True, output=format_skills_summary(skills))
    return CommandResult(handled=False)


def _print_block(title: str, body: str = "") -> None:
    width = _terminal_width()
    print(f"\n{_rule(width)}")
    print(title)
    print(_rule(width))
    if body:
        print(body)


def _wrap_output(text: str, *, width: int | None = None) -> str:
    width = width or _terminal_width()
    wrapped_lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        if not raw_line.strip():
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(textwrap.wrap(raw_line, width=width, replace_whitespace=False) or [""])
    return "\n".join(wrapped_lines)



def format_cache_stats_line(data: dict) -> str | None:
    """Phase 2: format a `cache_stats` event payload as the user-facing CLI line.

    Returns the bracketed line per clarification Q6:
        `[cache: <cached>K/<prompt>K cached, <pct>%]`
    where <cached> and <prompt> are integer kilotokens (// 1024) and <pct> is
    int(ratio * 100). Returns None when ratio is None — the caller must skip
    printing in that case (silent miss / no-usage-data).
    """
    ratio = data.get("ratio")
    if ratio is None:
        return None
    cached_tokens = int(data.get("cached") or 0)
    prompt_tokens = int(data.get("prompt") or 0)
    cached_k = cached_tokens // 1024
    prompt_k = prompt_tokens // 1024
    pct = int(ratio * 100)
    return f"[cache: {cached_k}K/{prompt_k}K cached, {pct}%]"


def main() -> None:
    from src.agent.loop import AgentLoop
    from src.agent.skills import SkillsLoader
    from src.agent.subagent import SubAgentContext
    from src.providers.chat import ChatLLM
    from src.memory.persistent import PersistentMemory
    from src.tools import build_registry
    from src.tools.delegate_tool import DelegateTool
    from src.tools.team_tool import TeamTool

    pm = PersistentMemory()
    llm = ChatLLM()
    registry = build_registry(persistent_memory=pm)
    skills_loader = SkillsLoader()
    skills = list(skills_loader.skills)

    parent_ctx = SubAgentContext(depth=0, parent_run_dir=RUNS_DIR, parent_session_id="cli")
    registry.register(DelegateTool(llm, registry, RUNS_DIR, parent_ctx))
    registry.register(TeamTool(llm, registry, RUNS_DIR, parent_ctx))

    provider = os.getenv("LANGCHAIN_PROVIDER", "default")
    model = llm.model_name or os.getenv("LANGCHAIN_MODEL_NAME", "default")
    print(format_banner(provider=provider, model=model, cwd=Path.cwd(), skills_count=len(skills)))
    print()

    stream_state = {"block": "", "streamed_text": False}

    def _begin_stream_block(title: str) -> None:
        if stream_state["block"] != title:
            _print_block(title)
            stream_state["block"] = title

    def _on_event(event_type: str, data: dict) -> None:
        if event_type == "thinking_delta":
            _begin_stream_block("Thinking")
            print(data.get("delta", ""), end="", flush=True)
        elif event_type == "text_delta":
            stream_state["streamed_text"] = True
            _begin_stream_block("Answer")
            print(data.get("delta", ""), end="", flush=True)
        elif event_type == "thinking_done":
            if stream_state["block"] == "Thinking":
                print()
        elif event_type == "tool_call":
            if stream_state["block"]:
                print()
                stream_state["block"] = ""
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
        elif event_type == "cache_stats":
            # Phase 2: per-turn cache hit-ratio indicator (Q6 format).
            # Silent when ratio is None — AgentLoop does not emit the event
            # in that case, but we double-check defensively.
            line = format_cache_stats_line(data)
            if line is not None:
                print(line, flush=True)

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
            user_input = input("mini-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        command = handle_builtin_command(user_input, history=history, skills=skills)
        if command.handled:
            if command.clear_screen:
                print("\033[2J\033[H", end="")
            if command.clear_history:
                history.clear()
            if command.output:
                print(command.output)
            if user_input.lower() in {"/quit", "/exit", "/q"}:
                break
            continue

        stream_state = {"block": "", "streamed_text": False}
        result = agent.run(user_message=user_input, history=history or None)
        status = result.get("status", "unknown")
        content = result.get("content", "")

        if stream_state["block"]:
            print()
            stream_state["block"] = ""

        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})

        _print_block(f"Agent [{status}]")
        if not stream_state["streamed_text"] and content:
            print(_wrap_output(content))
        print()

        if run_dir := result.get("run_dir"):
            print(f"run_dir: {run_dir}\n")


if __name__ == "__main__":
    main()
