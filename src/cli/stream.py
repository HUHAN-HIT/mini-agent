"""流式渲染器 + 瞬态思考 spinner（对接 AgentLoop event_callback）。

单 agent 模式：短生命周期 rich.Live（transient=True）驱动 spinner；工具事件与
最终回复以静态打印持久化。spinner 的 pause() 上下文管理器在静态打印前停掉 Live，
避免 ANSI 交错（nanobot 教训）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from src.cli.format import (
    beautify_tool_name,
    format_cache_stats_line,
    format_duration,
    summarize_args,
)
from src.cli.theme import Theme, get_console

_THINKING_VERBS = ("Thinking", "Reasoning", "Working", "Pondering", "Cooking")


def _pick_verb(seed: int) -> str:
    return _THINKING_VERBS[seed % len(_THINKING_VERBS)]


@dataclass
class _SpinnerState:
    verb: str
    started_at: float
    paused: bool = False
    stopped: bool = False
    extra: str = ""


class ThinkingSpinner:
    """可安全暂停/恢复的瞬态 spinner。"""

    _seed = 0

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or get_console()
        ThinkingSpinner._seed += 1
        self._state = _SpinnerState(verb=_pick_verb(ThinkingSpinner._seed),
                                    started_at=time.monotonic())
        from rich.live import Live  # 局部导入，import 期不建 Live
        self._Live = Live
        self._live: Optional[object] = None
        self._lock = threading.Lock()
        self._tick_thread: Optional[threading.Thread] = None

    def start(self, verb: str | None = None) -> None:
        with self._lock:
            if self._live is not None:
                return
            if verb:
                self._state.verb = verb
            self._state.started_at = time.monotonic()
            self._state.paused = False
            self._state.stopped = False
            self._live = self._Live(self._render(), console=self._console,
                                    refresh_per_second=10, transient=True)
            self._live.start(refresh=False)
            self._tick_thread = threading.Thread(target=self._tick, daemon=True)
            self._tick_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._state.stopped = True
            if self._live is not None:
                try:
                    self._live.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._live = None

    @contextmanager
    def pause(self) -> Iterator[None]:
        was_running = False
        with self._lock:
            if self._live is not None and not self._state.stopped:
                was_running = True
                self._state.paused = True
                try:
                    self._live.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._live = None
        try:
            yield
        finally:
            if was_running and not self._state.stopped:
                with self._lock:
                    self._state.paused = False
                    self._live = self._Live(self._render(), console=self._console,
                                            refresh_per_second=10, transient=True)
                    self._live.start(refresh=False)

    def _render(self) -> Text:
        elapsed_ms = int((time.monotonic() - self._state.started_at) * 1000)
        text = Text()
        text.append(" ")
        text.append("●", style=Theme.warning)
        text.append("  ")
        text.append(self._state.verb, style=Theme.primary_dim)
        text.append("   ")
        text.append(format_duration(elapsed_ms), style=Theme.muted)
        return text

    def _tick(self) -> None:
        while not self._state.stopped:
            time.sleep(0.1)
            with self._lock:
                if self._live is not None and not self._state.paused:
                    try:
                        self._live.update(self._render(), refresh=True)
                    except Exception:  # noqa: BLE001
                        pass


@dataclass
class _ToolCall:
    name: str
    args: dict | str | None
    started_at: float = field(default_factory=time.monotonic)


class StreamRenderer:
    """驱动单轮 agent 的流式显示。由 app 的 event_callback 调用 handle()。"""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or get_console()
        self._spinner: Optional[ThinkingSpinner] = None
        self._active: dict[str, _ToolCall] = {}
        self._block = ""  # "" | "thinking" | "answer"
        self._streamed_text = False

    @property
    def streamed_text(self) -> bool:
        return self._streamed_text

    def begin(self) -> None:
        self._active.clear()
        self._block = ""
        self._streamed_text = False
        self._spinner = ThinkingSpinner(self._console)
        self._spinner.start()

    def _emit(self, renderable) -> None:  # type: ignore[no-untyped-def]
        if self._spinner is not None:
            with self._spinner.pause():
                self._console.print(renderable)
        else:
            self._console.print(renderable)

    def handle(self, event_type: str, data: dict) -> None:
        if event_type == "thinking_delta":
            self._stream_inline("thinking", data.get("delta", ""), Theme.primary_dim)
        elif event_type == "text_delta":
            self._streamed_text = True
            self._stream_inline("answer", data.get("delta", ""), None)
        elif event_type == "thinking_done":
            if self._block == "thinking":
                self._console.print()
                self._block = ""
        elif event_type == "tool_call":
            self._active[data.get("tool", "?")] = _ToolCall(
                name=data.get("tool", "?"), args=data.get("arguments"))
        elif event_type == "tool_result":
            self._finish_block()
            self._emit(self.format_tool_line(
                data.get("tool", "?"),
                self._active.pop(data.get("tool", "?"), _ToolCall("?", None)).args,
                data.get("status", "?"),
                data.get("elapsed_ms", 0),
                (data.get("preview") or "")[:120].replace("\n", " "),
            ))
        elif event_type == "compact":
            self._emit(Text(f"[compact] triggered at {data.get('tokens_before', '?')} tokens",
                            style=Theme.muted))
        elif event_type == "cache_stats":
            line = format_cache_stats_line(data)
            if line is not None:
                self._emit(Text(line, style=Theme.muted))

    def _stream_inline(self, block: str, delta: str, style) -> None:  # type: ignore[no-untyped-def]
        """流式 token：spinner 让路，直接写 stdout（保持实时）。"""
        if self._block != block:
            self._finish_block()
            if self._spinner is not None:
                self._spinner.stop()
                self._spinner = None
            title = "Thinking" if block == "thinking" else "Answer"
            self._console.print(Text(title, style=Theme.label))
            self._block = block
        self._console.print(delta, end="", style=style, markup=False, highlight=False)

    def _finish_block(self) -> None:
        if self._block:
            self._console.print()
            self._block = ""

    def format_tool_line(self, tool: str, args, status: str,
                         elapsed_ms, preview: str) -> Text:  # type: ignore[no-untyped-def]
        line = Text()
        marker_style = Theme.success if status in {"ok", "success"} else Theme.danger
        line.append("●", style=marker_style)
        line.append("  ")
        line.append(beautify_tool_name(tool), style=Theme.label)
        args_preview = summarize_args(args if isinstance(args, (dict, str)) else None)
        if args_preview:
            line.append(" ")
            line.append(f"({args_preview})", style=Theme.muted)
        pad_to = 52
        line.append(" " * max(2, pad_to - len(line.plain)))
        dur = format_duration(elapsed_ms)
        if dur:
            line.append(dur, style=Theme.muted)
        if preview:
            line.append(" · ", style=Theme.muted)
            line.append(preview, style=Theme.muted)
        return line

    def finish(self, *, status: str, content: str, run_dir: str | None) -> None:
        self._finish_block()
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        if not self._streamed_text and content:
            head = Text()
            head.append("● ", style=Theme.primary)
            head.append(f"Agent [{status}]", style=Theme.label)
            self._console.print(head)
            self._console.print(Markdown(content))
        self._console.print()
        if run_dir:
            self._console.print(Text(f"run_dir: {run_dir}", style=Theme.muted))
            self._console.print()


__all__ = ["StreamRenderer", "ThinkingSpinner", "format_cache_stats_line"]
