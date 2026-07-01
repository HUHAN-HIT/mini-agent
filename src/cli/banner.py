"""启动横幅：紫色渐变 ASCII 字标 + 元信息行。"""

from __future__ import annotations

from typing import Final

from rich.console import Console
from rich.text import Text

from src.cli.theme import Theme

_LOGO: Final[tuple[str, ...]] = (
    r"                 _       _                            _   ",
    r"  _ __ ___ (_)_ __ (_)       __ _  __ _  ___ _ __ | |_ ",
    r" | '_ ` _ \| | '_ \| |_____ / _` |/ _` |/ _ \ '_ \| __|",
    r" | | | | | | | | | | |_____| (_| | (_| |  __/ | | | |_ ",
    r" |_| |_| |_|_|_| |_|_|      \__,_|\__, |\___|_| |_|\__|",
    r"                                  |___/                ",
)

_GRADIENT_START: Final[tuple[int, int, int]] = (0x7C, 0x3A, 0xED)  # violet-600
_GRADIENT_END: Final[tuple[int, int, int]] = (0xC4, 0xB5, 0xFD)    # violet-300


def _lerp(a: int, b: int, r: float) -> int:
    return round(a + (b - a) * r)


def _gradient_style(index: int, total: int) -> str:
    r = 0.0 if total <= 1 else index / (total - 1)
    red = _lerp(_GRADIENT_START[0], _GRADIENT_END[0], r)
    green = _lerp(_GRADIENT_START[1], _GRADIENT_END[1], r)
    blue = _lerp(_GRADIENT_START[2], _GRADIENT_END[2], r)
    return f"bold #{red:02x}{green:02x}{blue:02x}"


def _gradient_line(line: str) -> Text:
    text = Text()
    total = max(1, len(line.rstrip()))
    for idx, char in enumerate(line):
        text.append(char, style=None if char == " " else _gradient_style(idx, total))
    return text


def print_banner(console: Console, *, provider: str, model: str,
                 skills: int, version: str) -> None:
    """冷启动打印一次横幅。非彩色终端会自动降级为普通文本。"""
    console.print()
    for line in _LOGO:
        console.print(_gradient_line(line.rstrip()))
    meta = Text()
    meta.append(f"mini-agent v{version}", style=Theme.muted)
    meta.append("  ·  ", style=Theme.muted)
    meta.append(f"{provider or 'default'}", style=Theme.muted)
    meta.append("  ·  ", style=Theme.muted)
    meta.append(f"{model or 'default'}", style=Theme.muted)
    meta.append("  ·  ", style=Theme.muted)
    meta.append(f"skills:{skills}", style=Theme.muted)
    console.print(meta)
    console.print(Text("输入 /help 查看命令，Ctrl+D 退出。", style=Theme.muted))
    console.print()


__all__ = ["print_banner"]
