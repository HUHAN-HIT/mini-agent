"""SlashCompleter 补全测试。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _completions(text: str):
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent
    from src.cli.completer import SlashCompleter

    doc = Document(text=text, cursor_position=len(text))
    return list(SlashCompleter().get_completions(doc, CompleteEvent()))


def test_slash_triggers_completions() -> None:
    comps = _completions("/h")
    texts = {c.text for c in comps}
    assert "/help" in texts or "help" in texts


def test_prose_does_not_trigger() -> None:
    assert _completions("hello world") == []


def test_stops_after_space() -> None:
    assert _completions("/help ") == []
