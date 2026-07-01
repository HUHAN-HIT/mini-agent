"""Tests for the lightweight terminal UI helpers in cli.py."""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def test_banner_shows_runtime_context() -> None:
    from rich.console import Console

    from src.cli.banner import print_banner

    con = Console(record=True, no_color=True, width=100)
    print_banner(con, provider="openai", model="gpt-test", skills=12, version="0.1.0")
    out = con.export_text()

    assert "mini-agent" in out.lower()
    assert "openai" in out
    assert "gpt-test" in out
    assert "skills:12" in out


def test_help_lists_terminal_commands() -> None:
    import cli

    help_text = cli.format_help()

    for command in ["/help", "/clear", "/history", "/skills", "/quit"]:
        assert command in help_text


def test_history_summary_limits_recent_turns() -> None:
    import cli

    history = [
        {"role": "user", "content": "first user message"},
        {"role": "assistant", "content": "first assistant answer"},
        {"role": "user", "content": "second user message"},
        {"role": "assistant", "content": "second assistant answer"},
    ]

    rendered = cli.format_history_summary(history, limit=2)

    assert "second user message" in rendered
    assert "second assistant answer" in rendered
    assert "first user message" not in rendered


def test_command_dispatch_handles_builtin_commands() -> None:
    import cli

    history = [{"role": "user", "content": "hello"}]
    skills = [type("Skill", (), {"name": "example", "description": "Example skill"})()]

    clear = cli.handle_builtin_command("/clear", history=history, skills=skills)
    assert clear.handled is True
    assert clear.clear_history is True
    assert "/clear" in clear.output

    show_history = cli.handle_builtin_command("/history", history=history, skills=skills)
    assert show_history.handled is True
    assert "hello" in show_history.output

    unknown = cli.handle_builtin_command("normal prompt", history=history, skills=skills)
    assert unknown.handled is False
