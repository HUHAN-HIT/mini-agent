"""交互输入：TTY 用 prompt_toolkit（历史/补全/底部工具栏），否则退回 stdin。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


class InteractiveInput:
    """一行输入读取器。EOF→EOFError；中断→KeyboardInterrupt。"""

    def __init__(self, *, provider: str, model: str, skills: int,
                 history_path: Optional[Path] = None) -> None:
        self._provider = provider
        self._model = model
        self._skills = skills
        self.is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        self._session = None
        if self.is_tty:
            self._session = self._build_session(history_path)

    def _build_session(self, history_path: Optional[Path]):
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.formatted_text import HTML

            from src.cli.completer import SlashCompleter
            from src.cli.theme import Theme

            hp = history_path or (Path.home() / ".mini-agent" / "history")
            hp.parent.mkdir(parents=True, exist_ok=True)

            brand = getattr(Theme, "brand_hex", "#a78bfa")

            def bottom_toolbar():
                return HTML(
                    f" <b>{self._provider}</b> · {self._model} · skills:{self._skills} "
                )

            from prompt_toolkit.styles import Style as PTStyle
            style = PTStyle.from_dict({"prompt": f"{brand} bold"})

            return PromptSession(
                history=FileHistory(str(hp)),
                completer=SlashCompleter(),
                complete_while_typing=True,
                bottom_toolbar=bottom_toolbar,
                style=style,
            )
        except Exception:  # noqa: BLE001 — 任何构建失败都退回纯 input
            self.is_tty = False
            return None

    def read(self) -> str:
        if self._session is not None:
            from prompt_toolkit.formatted_text import HTML
            from src.cli.theme import Theme

            brand = getattr(Theme, "brand_hex", "#a78bfa")
            text = self._session.prompt(HTML(f"<style fg='{brand}'><b>mini-agent&gt; </b></style>"))
            return text.strip()
        # 非 TTY / 降级路径
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.strip()


__all__ = ["InteractiveInput"]
