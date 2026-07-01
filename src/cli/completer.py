"""prompt_toolkit 斜杠命令补全。仅当行首为 / 且命令 token 未含空格时触发。"""

from __future__ import annotations

from typing import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText

from src.cli.commands import SLASH_COMMANDS, Command, match_commands


def _primary_style() -> str:
    try:
        from src.cli.theme import Theme

        brand = getattr(Theme, "brand_hex", None)
        if isinstance(brand, str) and brand:
            return f"fg:{brand} bold"
    except Exception:  # noqa: BLE001
        pass
    return "fg:#a78bfa bold"


class SlashCompleter(Completer):
    """模糊匹配斜杠命令注册表。裸 / 列全部，/h 前缀过滤。"""

    def __init__(self, commands: Iterable[Command] = SLASH_COMMANDS) -> None:
        self._commands = tuple(commands)

    def get_completions(self, document: Document,
                        complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return
        slash_idx = text.index("/")
        token_zone = text[slash_idx + 1:]
        if " " in token_zone or "\t" in token_zone:
            return
        matches = match_commands(stripped)
        if not matches:
            return
        start_position = -len(token_zone)
        name_width = max((len(c.name) for c in matches), default=0) + 2
        primary, muted = _primary_style(), "fg:#9ca3af"
        for cmd in matches:
            display = FormattedText([
                (primary, cmd.name.ljust(name_width)),
                (muted, cmd.description),
            ])
            # 插入不含前导斜杠的命令名（斜杠已在缓冲区）。
            yield Completion(
                text=cmd.name.lstrip("/"),
                start_position=start_position,
                display=display,
                display_meta=cmd.description,
            )


__all__ = ["SlashCompleter"]
