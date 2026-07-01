"""mini-agent 交互 CLI 包。UI 全部在此，根 cli.py 仅为薄 shim。"""
from __future__ import annotations

from src.cli.app import main
from src.cli.commands import (
    CommandResult,
    format_help,
    format_history_summary,
    format_skills_summary,
    handle_builtin_command,
)
from src.cli.stream import format_cache_stats_line

__all__ = [
    "main",
    "format_cache_stats_line",
    "handle_builtin_command",
    "CommandResult",
    "format_help",
    "format_history_summary",
    "format_skills_summary",
]
