"""mini-agent CLI 的集中 Rich 样式表（紫色品牌）。

所有可见配色决定集中在此，保证风格一致。品牌紫：
* dark 终端: #a78bfa (primary) / #8b5cf6 (primary_dim)
* light 终端: #7c3aed (primary) / #6d28d9 (primary_dim)

暴露单例 Console（get_console），并遵守 NO_COLOR。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Final

from rich.console import Console
from rich.style import Style

BRAND_PURPLE_LIGHT: Final[str] = "#7c3aed"
BRAND_PURPLE_DARK: Final[str] = "#a78bfa"


def _is_dark_terminal(console: Console) -> bool:
    """尽力判断终端是否深色主题。"""
    override = os.environ.get("MINI_AGENT_THEME", "").strip().lower()
    if override in {"dark", "light"}:
        return override == "dark"
    if console.color_system is None:
        return True
    colorfgbg = os.environ.get("COLORFGBG", "")
    if ";" in colorfgbg:
        bg = colorfgbg.split(";")[-1].strip()
        if bg.isdigit():
            return int(bg) in {0, 1, 2, 3, 4, 5, 6, 7, 8}
    if os.environ.get("TERM_PROGRAM", "").lower() == "apple_terminal":
        return True
    return True


@dataclass(frozen=True)
class _ThemeStyles:
    primary: Style
    primary_dim: Style
    success: Style
    danger: Style
    warning: Style
    info: Style
    muted: Style
    bold: Style
    label: Style
    accent_bg: Style


_NO_COLOR: Final[bool] = "NO_COLOR" in os.environ


def _build_styles(dark: bool, no_color: bool) -> _ThemeStyles:
    """按当前终端模式构造样式集合。"""
    if no_color:
        return _ThemeStyles(
            primary=Style(bold=True),
            primary_dim=Style(),
            success=Style(),
            danger=Style(bold=True),
            warning=Style(),
            info=Style(),
            muted=Style(dim=True),
            bold=Style(bold=True),
            label=Style(bold=True),
            accent_bg=Style(reverse=True),
        )
    brand = BRAND_PURPLE_DARK if dark else BRAND_PURPLE_LIGHT
    brand_dim = "#8b5cf6" if dark else "#6d28d9"
    return _ThemeStyles(
        primary=Style(color=brand, bold=True),
        primary_dim=Style(color=brand_dim),
        success=Style(color="#16a34a", bold=True),
        danger=Style(color="#dc2626", bold=True),
        warning=Style(color="#d97706"),
        info=Style(color="#0891b2"),
        muted=Style(color="#9ca3af", dim=True) if dark else Style(color="#737373", dim=True),
        bold=Style(bold=True),
        label=Style(color="#d4d4d8", bold=True) if dark else Style(color="#525252", bold=True),
        accent_bg=Style(color=brand, reverse=True, bold=True),
    )


def _make_console() -> Console:
    """创建共享 Console。不强制 force_terminal，遵守 NO_COLOR，禁用 emoji。"""
    return Console(
        no_color=_NO_COLOR,
        soft_wrap=False,
        highlight=False,
        emoji=False,
        markup=True,
        stderr=False,
        legacy_windows=False if sys.platform == "win32" else None,
    )


_console: Console = _make_console()
_dark: bool = _is_dark_terminal(_console)
_styles: _ThemeStyles = _build_styles(_dark, _NO_COLOR or _console.color_system is None)


def get_console() -> Console:
    return _console


def is_dark() -> bool:
    return _dark


class Theme:
    """CLI 通用 Rich 样式命名空间。"""

    primary: Final[Style] = _styles.primary
    primary_dim: Final[Style] = _styles.primary_dim
    success: Final[Style] = _styles.success
    danger: Final[Style] = _styles.danger
    warning: Final[Style] = _styles.warning
    info: Final[Style] = _styles.info
    muted: Final[Style] = _styles.muted
    bold: Final[Style] = _styles.bold
    label: Final[Style] = _styles.label
    accent_bg: Final[Style] = _styles.accent_bg
    brand_hex: Final[str] = BRAND_PURPLE_DARK if _dark else BRAND_PURPLE_LIGHT


__all__ = [
    "Theme",
    "get_console",
    "is_dark",
    "BRAND_PURPLE_LIGHT",
    "BRAND_PURPLE_DARK",
]
