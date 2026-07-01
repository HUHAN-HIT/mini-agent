"""theme.py 测试：NO_COLOR 降级、深浅色覆盖、品牌紫。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_no_color_produces_plain_styles() -> None:
    from src.cli.theme import _build_styles

    styles = _build_styles(dark=True, no_color=True)
    # 无色模式下 primary 只加粗、不带颜色
    assert styles.primary.color is None
    assert styles.primary.bold is True
    assert styles.success.color is None


def test_colored_primary_uses_purple() -> None:
    from src.cli.theme import _build_styles, BRAND_PURPLE_DARK, BRAND_PURPLE_LIGHT

    dark = _build_styles(dark=True, no_color=False)
    light = _build_styles(dark=False, no_color=False)
    assert dark.primary.color.name.lower() == BRAND_PURPLE_DARK.lower()
    assert light.primary.color.name.lower() == BRAND_PURPLE_LIGHT.lower()


def test_theme_override_env(monkeypatch) -> None:
    from rich.console import Console
    from src.cli.theme import _is_dark_terminal

    monkeypatch.setenv("MINI_AGENT_THEME", "light")
    assert _is_dark_terminal(Console()) is False
    monkeypatch.setenv("MINI_AGENT_THEME", "dark")
    assert _is_dark_terminal(Console()) is True
