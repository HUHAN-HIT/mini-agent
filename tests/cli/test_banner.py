"""banner.py 输出测试。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_banner_prints_wordmark_and_meta() -> None:
    from rich.console import Console
    from src.cli.banner import print_banner

    con = Console(record=True, force_terminal=False, no_color=True, width=100)
    print_banner(con, provider="openai", model="gpt-test", skills=12, version="0.1.0")
    out = con.export_text()
    assert "mini-agent" in out.lower()
    assert "openai" in out
    assert "gpt-test" in out
    assert "12" in out
