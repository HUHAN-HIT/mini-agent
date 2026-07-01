"""StreamRenderer 渲染测试（不触碰真实 spinner 线程）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_tool_line_contains_name_and_duration() -> None:
    from src.cli.stream import StreamRenderer

    r = StreamRenderer()
    line = r.format_tool_line("web_search", {"query": "btc"}, "ok", 1500, "3 results")
    plain = line.plain
    assert "Web Search" in plain
    assert "btc" in plain
    assert "1.5s" in plain
    assert "3 results" in plain


def test_finish_prints_markdown_when_not_streamed(capsys) -> None:
    from src.cli.stream import StreamRenderer

    r = StreamRenderer()
    r._streamed_text = False  # 模拟未流式
    r.finish(status="success", content="# Title\nbody", run_dir=None)
    out = capsys.readouterr().out
    assert "Title" in out


def test_handle_text_delta_sets_streamed_flag() -> None:
    from src.cli.stream import StreamRenderer

    r = StreamRenderer()
    r.begin()
    r.handle("text_delta", {"delta": "hello"})
    assert r.streamed_text is True
    r.finish(status="success", content="hello", run_dir=None)
