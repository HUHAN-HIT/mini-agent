"""斜杠命令注册表 + 处理器。命令本地拦截，不进 LLM。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    description: str


SLASH_COMMANDS: tuple[Command, ...] = (
    Command("/help", (), "显示命令列表"),
    Command("/clear", (), "清屏并重置本会话内存历史"),
    Command("/history", (), "显示近期对话轮次"),
    Command("/skills", (), "列出已加载的 skills"),
    Command("/quit", ("/exit", "/q"), "退出 CLI"),
)


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    output: str = ""
    clear_history: bool = False
    clear_screen: bool = False
    quit: bool = False


def match_commands(text: str) -> list[Command]:
    """按前缀/子串匹配命令（用于补全）。空/裸 `/` 返回全部。"""
    token = text.strip().lstrip("/").split(" ", 1)[0].lower()
    if not token:
        return list(SLASH_COMMANDS)
    pref = [c for c in SLASH_COMMANDS if c.name.lstrip("/").startswith(token)]
    if pref:
        return pref
    return [c for c in SLASH_COMMANDS if token in c.name.lstrip("/")]


def _truncate(text: str, max_len: int = 96) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= max_len else clean[: max_len - 3].rstrip() + "..."


def format_help() -> str:
    lines = ["Commands"]
    for cmd in SLASH_COMMANDS:
        alias = f"  ({', '.join(cmd.aliases)})" if cmd.aliases else ""
        lines.append(f"  {cmd.name:<10} {cmd.description}{alias}")
    return "\n".join(lines)


def format_history_summary(history: list[dict[str, Any]], *, limit: int = 8) -> str:
    if not history:
        return "No conversation history yet."
    recent = history[-limit:]
    lines = [f"Recent History ({len(recent)}/{len(history)} messages)"]
    start = max(1, len(history) - len(recent) + 1)
    for idx, msg in enumerate(recent, start=start):
        role = str(msg.get("role") or "?")
        content = _truncate(str(msg.get("content") or ""), 110)
        lines.append(f"{idx:>3}. {role:<9} {content}")
    return "\n".join(lines)


def format_skills_summary(skills: list[Any], *, limit: int = 80) -> str:
    if not skills:
        return "No skills loaded."
    lines = [f"Skills ({len(skills)})"]
    for skill in skills[:limit]:
        name = getattr(skill, "name", "?")
        desc = getattr(skill, "description", "")
        lines.append(f"  {name:<24} {_truncate(desc, 86)}")
    if len(skills) > limit:
        lines.append(f"  ... {len(skills) - limit} more")
    return "\n".join(lines)


def handle_builtin_command(command: str, *, history: list[dict[str, Any]],
                           skills: list[Any]) -> CommandResult:
    normalized = command.strip().lower()
    quit_names = {"/quit", "/exit", "/q"}
    if normalized in quit_names:
        return CommandResult(handled=True, output="Goodbye.", quit=True)
    if normalized == "/help":
        return CommandResult(handled=True, output=format_help())
    if normalized == "/clear":
        return CommandResult(handled=True,
                             output="Screen cleared. In-memory history reset.",
                             clear_history=True, clear_screen=True)
    if normalized == "/history":
        return CommandResult(handled=True, output=format_history_summary(history))
    if normalized == "/skills":
        return CommandResult(handled=True, output=format_skills_summary(skills))
    return CommandResult(handled=False)


__all__ = [
    "Command", "SLASH_COMMANDS", "match_commands", "CommandResult",
    "handle_builtin_command", "format_help", "format_history_summary",
    "format_skills_summary",
]
