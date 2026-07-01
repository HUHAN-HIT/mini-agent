"""app.py REPL 循环测试（mock agent，不触网）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class _FakeReader:
    def __init__(self, lines):
        self._it = iter(lines)

    def read(self):
        try:
            return next(self._it)
        except StopIteration:
            raise EOFError


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run(self, *, user_message, history=None, **kw):
        self.calls.append(user_message)
        return {"status": "success", "content": f"echo:{user_message}", "run_dir": None}


def test_repl_runs_user_turn_then_quits() -> None:
    from rich.console import Console
    from src.cli.app import run_repl
    from src.cli.stream import StreamRenderer

    agent = _FakeAgent()
    reader = _FakeReader(["hello", "/quit"])
    console = Console(no_color=True, record=True)
    history: list[dict] = []

    run_repl(agent=agent, skills=[], renderer=StreamRenderer(console),
             reader=reader, console=console, history=history)

    assert agent.calls == ["hello"]
    assert any(m["content"] == "echo:hello" for m in history)


def test_repl_slash_help_does_not_call_agent() -> None:
    from rich.console import Console
    from src.cli.app import run_repl
    from src.cli.stream import StreamRenderer

    agent = _FakeAgent()
    reader = _FakeReader(["/help", "/quit"])
    console = Console(no_color=True, record=True)

    run_repl(agent=agent, skills=[], renderer=StreamRenderer(console),
             reader=reader, console=console, history=[])

    assert agent.calls == []
    assert "/help" in console.export_text()
