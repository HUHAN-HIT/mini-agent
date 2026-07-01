"""InteractiveInput 测试：非 TTY 降级路径。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_non_tty_falls_back_to_stdin(monkeypatch) -> None:
    from src.cli.input import InteractiveInput

    monkeypatch.setattr(sys, "stdin", io.StringIO("hello agent\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    inp = InteractiveInput(provider="openai", model="m", skills=3)
    assert inp.is_tty is False
    assert inp.read() == "hello agent"


def test_non_tty_eof_raises(monkeypatch) -> None:
    from src.cli.input import InteractiveInput

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    inp = InteractiveInput(provider="openai", model="m", skills=0)
    try:
        inp.read()
        assert False, "expected EOFError"
    except EOFError:
        pass
