"""commands.py 斜杠命令测试。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_help_lists_all_commands() -> None:
    from src.cli.commands import format_help

    text = format_help()
    for c in ["/help", "/clear", "/history", "/skills", "/quit"]:
        assert c in text


def test_dispatch_quit_and_clear() -> None:
    from src.cli.commands import handle_builtin_command

    q = handle_builtin_command("/quit", history=[], skills=[])
    assert q.handled and q.quit is True

    c = handle_builtin_command("/clear", history=[{"role": "user", "content": "x"}], skills=[])
    assert c.handled and c.clear_history and c.clear_screen

    unknown = handle_builtin_command("just a prompt", history=[], skills=[])
    assert unknown.handled is False


def test_history_summary_limit() -> None:
    from src.cli.commands import handle_builtin_command

    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "second"},
    ]
    res = handle_builtin_command("/history", history=history, skills=[])
    assert res.handled and "second" in res.output


def test_match_commands_typeahead() -> None:
    from src.cli.commands import match_commands

    names = {c.name for c in match_commands("/h")}
    assert "/help" in names and "/history" in names
    assert match_commands("/skills") and match_commands("/skills")[0].name == "/skills"
