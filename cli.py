#!/usr/bin/env python3
"""mini-agent 交互 CLI —— 薄入口 shim。真实实现见 src/cli/。

保留 `python cli.py` 用法，并向后兼容再导出旧公共符号
（format_cache_stats_line / handle_builtin_command / ...）。
"""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from src.cli.app import main  # noqa: E402
from src.cli.commands import (  # noqa: E402,F401
    CommandResult,
    format_help,
    format_history_summary,
    format_skills_summary,
    handle_builtin_command,
)
from src.cli.stream import format_cache_stats_line  # noqa: E402,F401

__all__ = [
    "main",
    "format_cache_stats_line",
    "handle_builtin_command",
    "CommandResult",
    "format_help",
    "format_history_summary",
    "format_skills_summary",
]

if __name__ == "__main__":
    main()
