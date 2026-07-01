"""format.py 纯函数测试。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_CACHE_LINE_RE = re.compile(r"^\[cache: \d+K/\d+K cached, \d+%\]$")


def test_format_duration() -> None:
    from src.cli.format import format_duration

    assert format_duration(None) == ""
    assert format_duration(820) == "820ms"
    assert format_duration(1500) == "1.5s"


def test_beautify_tool_name() -> None:
    from src.cli.format import beautify_tool_name

    assert beautify_tool_name("web_search") == "Web Search"
    assert beautify_tool_name("read_url") == "Read Url" or beautify_tool_name("read_url") == "Read URL"
    assert beautify_tool_name("get_market_data") == "Market Data"


def test_summarize_args_prefers_priority_key() -> None:
    from src.cli.format import summarize_args

    assert summarize_args({"query": "hello world"}) == '"hello world"'
    assert summarize_args(None) == ""
    long = summarize_args({"query": "x" * 200})
    assert long.endswith('…"') and len(long) <= 62


def test_cache_line_format() -> None:
    from src.cli.format import format_cache_stats_line

    line = format_cache_stats_line({"cached": 4200, "prompt": 5119, "ratio": 0.827})
    assert isinstance(line, str) and _CACHE_LINE_RE.match(line)
    assert format_cache_stats_line({"ratio": None}) is None
    sparse = format_cache_stats_line({"ratio": 0.5})
    assert _CACHE_LINE_RE.match(sparse)
