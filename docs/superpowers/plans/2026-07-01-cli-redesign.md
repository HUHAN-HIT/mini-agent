# mini-agent CLI 改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 mini-agent 的交互 CLI 全面对齐 Vibe-Trading —— 基于 rich + prompt_toolkit 的美化终端（紫色品牌）、首次运行 onboarding 向导，并新增 `mini-agent` 命令入口与 uv 优先安装流程。

**Architecture:** UI 逻辑全部进新包 `src/cli/`（theme/banner/stream/commands/completer/input/onboard/app），根 `cli.py` 退化为薄 shim（保留 `python cli.py` 并暴露 `main` + 向后兼容再导出）。复用现有 `AgentLoop` 的 `event_callback` 流式事件契约、`ChatLLM`、`registry`、`SkillsLoader`、`PersistentMemory`，以及 `src/providers/llm.py` 的 provider 映射。

**Tech Stack:** Python ≥3.11, rich ≥13, prompt_toolkit ≥3, setuptools, uv, pytest, ruff。

## Global Constraints

- 仓库根目录：`E:/03_个人项目归档/mini-agent`（下称 **REPO**）。所有相对路径以此为根。
- Python ≥ 3.11；新依赖：`rich>=13.0`、`prompt_toolkit>=3.0`。
- 项目规则：**代码/注释用中文，界面文案无 emoji**（`Console(emoji=False)`）。
- 品牌主色（紫）：dark 终端 `#a78bfa`（primary）/ `#8b5cf6`（primary_dim）；light 终端 `#7c3aed`（primary）/ `#6d28d9`（primary_dim）。
- 测试统一用 `uv run pytest <path> -v` 运行；新测试放 `tests/cli/`，每个测试文件顶部插入 REPO 到 `sys.path`。
- 保留向后兼容符号：`cli.format_cache_stats_line`、`cli.handle_builtin_command`、`cli.CommandResult`、`cli.format_help`、`cli.format_history_summary`、`cli.format_skills_summary`、`cli.main`。
- 提交规范：每个 Task 末尾提交一次；提交信息用中文，结尾追加
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- AgentLoop 事件契约（`event_callback(event_type, data)`，不可更改）：
  - `thinking_delta` `{delta}` / `text_delta` `{delta}` / `thinking_done` `{}`
  - `tool_call` `{tool, arguments}` / `tool_result` `{tool, status, elapsed_ms, preview}`
  - `compact` `{tokens_before}` / `cache_stats` `{ratio, cached, prompt}`

---

### Task 1: 安装依赖 + 创建 `src/cli/` 包骨架 + theme.py

**Files:**
- Modify: `REPO/pyproject.toml`（新增 rich 依赖）
- Create: `REPO/src/cli/__init__.py`
- Create: `REPO/src/cli/theme.py`
- Create: `REPO/tests/cli/__init__.py`
- Test: `REPO/tests/cli/test_theme.py`

**Interfaces:**
- Produces:
  - `src.cli.theme.get_console() -> rich.console.Console`
  - `src.cli.theme.is_dark() -> bool`
  - `src.cli.theme.Theme`（类属性 `primary/primary_dim/success/danger/warning/info/muted/bold/label/accent_bg`（均为 `rich.style.Style`）+ `brand_hex: str`）
  - `src.cli.theme._is_dark_terminal(console) -> bool`（内部，供测试）
  - `src.cli.theme._build_styles(dark: bool, no_color: bool) -> _ThemeStyles`（内部，供测试）
  - 常量 `BRAND_PURPLE_LIGHT="#7c3aed"`, `BRAND_PURPLE_DARK="#a78bfa"`

- [ ] **Step 1: 安装 rich（写入 pyproject + lock + venv）**

Run:
```bash
cd "E:/03_个人项目归档/mini-agent" && uv add "rich>=13.0"
```
Expected: `pyproject.toml` 的 `[project].dependencies` 出现 `rich>=13.0`；`uv.lock` 更新；安装成功。

- [ ] **Step 2: 创建包骨架文件**

Create `REPO/src/cli/__init__.py`（本任务先留最小内容，Task 10 再补 `main` 与再导出）:
```python
"""mini-agent 交互 CLI 包。UI 全部在此，根 cli.py 仅为薄 shim。"""
from __future__ import annotations

__all__: list[str] = []
```

Create `REPO/tests/cli/__init__.py`（空文件）:
```python
```

- [ ] **Step 3: 写失败测试 `tests/cli/test_theme.py`**

```python
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
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_theme.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.cli.theme'`）

- [ ] **Step 5: 实现 `src/cli/theme.py`**

```python
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
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_theme.py -v`
Expected: PASS（3 passed）

- [ ] **Step 7: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add pyproject.toml uv.lock src/cli/__init__.py src/cli/theme.py tests/cli/__init__.py tests/cli/test_theme.py
git commit -m "feat(cli): 新增 src/cli/theme.py 紫色主题 + rich 依赖

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: stream.py 纯函数 + cache 行格式化

**Files:**
- Create: `REPO/src/cli/format.py`（时长/截断/工具名/参数摘要/cache 行）
- Test: `REPO/tests/cli/test_format.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `src.cli.format.format_duration(ms: int | float | None) -> str`
  - `src.cli.format.truncate(value: str, max_len: int) -> str`
  - `src.cli.format.beautify_tool_name(raw: str) -> str`
  - `src.cli.format.summarize_args(args: dict | str | None, *, max_len: int = 60) -> str`
  - `src.cli.format.format_cache_stats_line(data: dict) -> str | None`

- [ ] **Step 1: 写失败测试 `tests/cli/test_format.py`**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_format.py -v`
Expected: FAIL（`No module named 'src.cli.format'`）

- [ ] **Step 3: 实现 `src/cli/format.py`**

```python
"""CLI 展示层纯函数：时长、截断、工具名美化、参数摘要、cache 行。"""

from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^(get|run|do|fetch|load|build|compute|calc(?:ulate)?)_")

# 白名单内的缩写渲染为全大写，其余 Title-Case。
_ACRONYMS: frozenset[str] = frozenset({
    "api", "url", "csv", "json", "yaml", "sql", "html", "http", "pdf",
    "ai", "ml", "id", "os", "io",
})


def format_duration(ms: int | float | None) -> str:
    """毫秒 → 人读时长：`820ms` / `1.5s` / `2m3s`。"""
    if ms is None:
        return ""
    ms = int(ms)
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = seconds - minutes * 60
    return f"{minutes}m{rem:.0f}s"


def truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 1:
        return "…"
    return value[: max_len - 1] + "…"


def beautify_tool_name(raw: str) -> str:
    """`get_market_data` → `Market Data`；白名单缩写大写。"""
    if not raw:
        return raw
    parts = _PREFIX_RE.sub("", raw).split("_")
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        out.append(part.upper() if part.lower() in _ACRONYMS else part.capitalize())
    return " ".join(out) if out else raw


def summarize_args(args: dict | str | None, *, max_len: int = 60) -> str:
    """单行参数预览：优先 query/prompt/url/…；过长截断。"""
    if not args:
        return ""
    if isinstance(args, str):
        return truncate(args, max_len)
    if not isinstance(args, dict):
        return truncate(str(args), max_len)
    for priority_key in ("query", "prompt", "url", "path", "command", "content"):
        if priority_key in args and args[priority_key]:
            return f'"{truncate(str(args[priority_key]), max_len - 2)}"'
    pieces: list[str] = []
    used = 0
    for k, v in args.items():
        token = f"{k}={truncate(str(v), 20)}"
        if used + len(token) + 2 > max_len:
            pieces.append("…")
            break
        pieces.append(token)
        used += len(token) + 2
    return ", ".join(pieces)


def format_cache_stats_line(data: dict) -> str | None:
    """`cache_stats` 事件 → `[cache: <c>K/<p>K cached, <pct>%]`；ratio None 返回 None。"""
    ratio = data.get("ratio")
    if ratio is None:
        return None
    cached_tokens = int(data.get("cached") or 0)
    prompt_tokens = int(data.get("prompt") or 0)
    cached_k = cached_tokens // 1024
    prompt_k = prompt_tokens // 1024
    pct = int(ratio * 100)
    return f"[cache: {cached_k}K/{prompt_k}K cached, {pct}%]"


__all__ = [
    "format_duration",
    "truncate",
    "beautify_tool_name",
    "summarize_args",
    "format_cache_stats_line",
]
```

> 注：`beautify_tool_name("read_url")` 会得到 `Read URL`（`url` 在白名单）。测试的 `or` 分支已兼容。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_format.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/format.py tests/cli/test_format.py
git commit -m "feat(cli): 新增 format.py 展示层纯函数（时长/工具名/参数/cache 行）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: stream.py 流式渲染器 + 思考 spinner

**Files:**
- Create: `REPO/src/cli/stream.py`
- Test: `REPO/tests/cli/test_stream.py`

**Interfaces:**
- Consumes: `src.cli.theme.get_console/Theme`、`src.cli.format.*`
- Produces:
  - `src.cli.stream.ThinkingSpinner`（`start(verb=None)/stop()/pause()（contextmanager）`）
  - `src.cli.stream.StreamRenderer`：
    - `__init__(self, console=None)`
    - `begin() -> None`（开始一轮：启动 spinner，重置状态）
    - `handle(event_type: str, data: dict) -> None`（处理单个 AgentLoop 事件）
    - `finish(*, status: str, content: str, run_dir: str | None) -> None`（收尾：停 spinner；若未流式过文本则以 Markdown 打印 content；打印 run_dir）
    - `streamed_text: bool`（属性，是否流式过 text_delta）
    - `format_tool_line(tool, args, status, elapsed_ms, preview) -> rich.text.Text`（纯格式化，供测试）
  - 复用 `format_cache_stats_line`（从 format 再导出）

- [ ] **Step 1: 写失败测试 `tests/cli/test_stream.py`**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_stream.py -v`
Expected: FAIL（`No module named 'src.cli.stream'`）

- [ ] **Step 3: 实现 `src/cli/stream.py`**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_stream.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/stream.py tests/cli/test_stream.py
git commit -m "feat(cli): 新增 stream.py 流式渲染器 + 思考 spinner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: commands.py 斜杠命令注册表 + 处理器（含向后兼容）

**Files:**
- Create: `REPO/src/cli/commands.py`
- Test: `REPO/tests/cli/test_commands.py`

**Interfaces:**
- Consumes: 无（纯逻辑）
- Produces:
  - `src.cli.commands.Command`（frozen dataclass：`name: str`, `aliases: tuple[str,...]`, `description: str`）
  - `src.cli.commands.SLASH_COMMANDS: tuple[Command, ...]`
  - `src.cli.commands.match_commands(text: str) -> list[Command]`（前缀/子串匹配，供补全）
  - `src.cli.commands.CommandResult`（frozen dataclass：`handled: bool`, `output: str=""`, `clear_history: bool=False`, `clear_screen: bool=False`, `quit: bool=False`）
  - `src.cli.commands.handle_builtin_command(command: str, *, history: list[dict], skills: list) -> CommandResult`
  - `src.cli.commands.format_help() -> str`
  - `src.cli.commands.format_history_summary(history: list[dict], *, limit: int = 8) -> str`
  - `src.cli.commands.format_skills_summary(skills: list, *, limit: int = 80) -> str`

- [ ] **Step 1: 写失败测试 `tests/cli/test_commands.py`**

```python
"""commands.py 斜杠命令测试。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_help_lists_all_commands() -> None:
    from src.cli.commands import format_help

    text = format_help()
    for c in ["/help", "/clear", "/history", "/skills", "/quit"]:
        assert c in text


def test_dispatch_quit_and_clear() -> None:
    from src.cli.commands import handle_builtin_command

    q = handle_builtin_command("/quit", history=[], skills=[])
    assert q.handled and q.quit is True

    c = handle_builtin_command("/clear", history=[{"role": "user", "content": "x"}], skills=[])
    assert c.handled and c.clear_history and c.clear_screen

    unknown = handle_builtin_command("just a prompt", history=[], skills=[])
    assert unknown.handled is False


def test_history_summary_limit() -> None:
    from src.cli.commands import handle_builtin_command

    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "second"},
    ]
    res = handle_builtin_command("/history", history=history, skills=[])
    assert res.handled and "second" in res.output


def test_match_commands_typeahead() -> None:
    from src.cli.commands import match_commands

    names = {c.name for c in match_commands("/h")}
    assert "/help" in names and "/history" in names
    assert match_commands("/skills") and match_commands("/skills")[0].name == "/skills"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_commands.py -v`
Expected: FAIL（`No module named 'src.cli.commands'`）

- [ ] **Step 3: 实现 `src/cli/commands.py`**

```python
"""斜杠命令注册表 + 处理器。命令本地拦截，不进 LLM。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    description: str


SLASH_COMMANDS: tuple[Command, ...] = (
    Command("/help", (), "显示命令列表"),
    Command("/clear", (), "清屏并重置本会话内存历史"),
    Command("/history", (), "显示近期对话轮次"),
    Command("/skills", (), "列出已加载的 skills"),
    Command("/quit", ("/exit", "/q"), "退出 CLI"),
)


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    output: str = ""
    clear_history: bool = False
    clear_screen: bool = False
    quit: bool = False


def match_commands(text: str) -> list[Command]:
    """按前缀/子串匹配命令（用于补全）。空/裸 `/` 返回全部。"""
    token = text.strip().lstrip("/").split(" ", 1)[0].lower()
    if not token:
        return list(SLASH_COMMANDS)
    pref = [c for c in SLASH_COMMANDS if c.name.lstrip("/").startswith(token)]
    if pref:
        return pref
    return [c for c in SLASH_COMMANDS if token in c.name.lstrip("/")]


def _truncate(text: str, max_len: int = 96) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= max_len else clean[: max_len - 3].rstrip() + "..."


def format_help() -> str:
    lines = ["Commands"]
    for cmd in SLASH_COMMANDS:
        alias = f"  ({', '.join(cmd.aliases)})" if cmd.aliases else ""
        lines.append(f"  {cmd.name:<10} {cmd.description}{alias}")
    return "\n".join(lines)


def format_history_summary(history: list[dict[str, Any]], *, limit: int = 8) -> str:
    if not history:
        return "No conversation history yet."
    recent = history[-limit:]
    lines = [f"Recent History ({len(recent)}/{len(history)} messages)"]
    start = max(1, len(history) - len(recent) + 1)
    for idx, msg in enumerate(recent, start=start):
        role = str(msg.get("role") or "?")
        content = _truncate(str(msg.get("content") or ""), 110)
        lines.append(f"{idx:>3}. {role:<9} {content}")
    return "\n".join(lines)


def format_skills_summary(skills: list[Any], *, limit: int = 80) -> str:
    if not skills:
        return "No skills loaded."
    lines = [f"Skills ({len(skills)})"]
    for skill in skills[:limit]:
        name = getattr(skill, "name", "?")
        desc = getattr(skill, "description", "")
        lines.append(f"  {name:<24} {_truncate(desc, 86)}")
    if len(skills) > limit:
        lines.append(f"  ... {len(skills) - limit} more")
    return "\n".join(lines)


def handle_builtin_command(command: str, *, history: list[dict[str, Any]],
                           skills: list[Any]) -> CommandResult:
    normalized = command.strip().lower()
    quit_names = {"/quit", "/exit", "/q"}
    if normalized in quit_names:
        return CommandResult(handled=True, output="Goodbye.", quit=True)
    if normalized == "/help":
        return CommandResult(handled=True, output=format_help())
    if normalized == "/clear":
        return CommandResult(handled=True,
                             output="已清屏，本会话内存历史已重置 (/clear)",
                             clear_history=True, clear_screen=True)
    if normalized == "/history":
        return CommandResult(handled=True, output=format_history_summary(history))
    if normalized == "/skills":
        return CommandResult(handled=True, output=format_skills_summary(skills))
    return CommandResult(handled=False)


__all__ = [
    "Command", "SLASH_COMMANDS", "match_commands", "CommandResult",
    "handle_builtin_command", "format_help", "format_history_summary",
    "format_skills_summary",
]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_commands.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/commands.py tests/cli/test_commands.py
git commit -m "feat(cli): 新增 commands.py 斜杠命令注册表与处理器

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: completer.py 斜杠命令补全（引入 prompt_toolkit）

**Files:**
- Modify: `REPO/pyproject.toml`（新增 prompt_toolkit 依赖）
- Create: `REPO/src/cli/completer.py`
- Test: `REPO/tests/cli/test_completer.py`

**Interfaces:**
- Consumes: `src.cli.commands.SLASH_COMMANDS/match_commands`、`src.cli.theme.Theme.brand_hex`
- Produces: `src.cli.completer.SlashCompleter`（`prompt_toolkit.completion.Completer` 子类）

- [ ] **Step 1: 安装 prompt_toolkit**

Run:
```bash
cd "E:/03_个人项目归档/mini-agent" && uv add "prompt_toolkit>=3.0"
```
Expected: pyproject 依赖新增 `prompt_toolkit>=3.0`；lock/venv 更新。

- [ ] **Step 2: 写失败测试 `tests/cli/test_completer.py`**

```python
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_completer.py -v`
Expected: FAIL（`No module named 'src.cli.completer'`）

- [ ] **Step 4: 实现 `src/cli/completer.py`**

```python
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_completer.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add pyproject.toml uv.lock src/cli/completer.py tests/cli/test_completer.py
git commit -m "feat(cli): 新增 completer.py 斜杠补全 + prompt_toolkit 依赖

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: banner.py 启动横幅

**Files:**
- Create: `REPO/src/cli/banner.py`
- Test: `REPO/tests/cli/test_banner.py`

**Interfaces:**
- Consumes: `src.cli.theme.Theme/get_console`
- Produces: `src.cli.banner.print_banner(console, *, provider: str, model: str, skills: int, version: str) -> None`

- [ ] **Step 1: 写失败测试 `tests/cli/test_banner.py`**

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_banner.py -v`
Expected: FAIL（`No module named 'src.cli.banner'`）

- [ ] **Step 3: 实现 `src/cli/banner.py`**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_banner.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/banner.py tests/cli/test_banner.py
git commit -m "feat(cli): 新增 banner.py 紫色渐变启动横幅

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 暴露 provider 映射 + onboard.py 首次运行向导

**Files:**
- Modify: `REPO/src/providers/llm.py`（把局部 `_PROVIDER_MAP` 提升为模块级 `PROVIDER_ENV_MAP`）
- Create: `REPO/src/cli/onboard.py`
- Test: `REPO/tests/cli/test_onboard.py`

**Interfaces:**
- Consumes: `src.providers.llm.PROVIDER_ENV_MAP`
- Produces:
  - `src.cli.onboard.PROVIDERS: tuple[ProviderInfo, ...]`（`ProviderInfo` frozen dataclass：`key: str`, `label: str`, `key_env: str | None`, `base_env: str`, `default_model: str`）
  - `src.cli.onboard.build_env_updates(provider: str, model: str, api_key: str, base_url: str = "") -> dict[str, str]`
  - `src.cli.onboard.merge_env_file(path: Path, updates: dict[str, str]) -> None`（合并写、chmod 600）
  - `src.cli.onboard.needs_onboarding() -> bool`
  - `src.cli.onboard.run_onboarding(console, *, prompt_fn=None, env_path=None) -> Path`（交互向导；写文件 + 同步 os.environ；返回写入路径）
  - 常量 `DEFAULT_ENV_PATH = Path.home()/".mini-agent"/".env"`

- [ ] **Step 1: 把 provider 映射提升为模块级（修改 `src/providers/llm.py`）**

在 `llm.py` 中，将 `_sync_provider_env` 内的 `_PROVIDER_MAP` 局部字典删除，替换为读取模块级常量。具体改动：

在文件 `AGENT_DIR = Path(__file__).resolve().parents[2]` 之后（约第 72 行后）新增模块级常量：
```python
# provider -> (api_key 环境变量名 | None, base_url 环境变量名)
PROVIDER_ENV_MAP: dict[str, tuple[str | None, str]] = {
    "openai":     ("OPENAI_API_KEY",     "OPENAI_BASE_URL"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"),
    "deepseek":   ("DEEPSEEK_API_KEY",   "DEEPSEEK_BASE_URL"),
    "gemini":     ("GEMINI_API_KEY",     "GEMINI_BASE_URL"),
    "groq":       ("GROQ_API_KEY",       "GROQ_BASE_URL"),
    "dashscope":  ("DASHSCOPE_API_KEY",  "DASHSCOPE_BASE_URL"),
    "qwen":       ("DASHSCOPE_API_KEY",  "DASHSCOPE_BASE_URL"),
    "zhipu":      ("ZHIPU_API_KEY",      "ZHIPU_BASE_URL"),
    "moonshot":   ("MOONSHOT_API_KEY",   "MOONSHOT_BASE_URL"),
    "minimax":    ("MINIMAX_API_KEY",    "MINIMAX_BASE_URL"),
    "ollama":     (None,                 "OLLAMA_BASE_URL"),
}
```

然后把 `_sync_provider_env` 函数体内的这段：
```python
    _PROVIDER_MAP: dict[str, tuple[str | None, str]] = {
        "openai":     ("OPENAI_API_KEY",     "OPENAI_BASE_URL"),
        ...
        "ollama":     (None,                  "OLLAMA_BASE_URL"),
    }

    spec = _PROVIDER_MAP.get(provider, _PROVIDER_MAP["openai"])
```
替换为：
```python
    spec = PROVIDER_ENV_MAP.get(provider, PROVIDER_ENV_MAP["openai"])
```

- [ ] **Step 2: 写失败测试 `tests/cli/test_onboard.py`**

```python
"""onboard.py 向导测试（不触发真实交互）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_providers_derived_from_llm_map() -> None:
    from src.cli.onboard import PROVIDERS
    from src.providers.llm import PROVIDER_ENV_MAP

    keys = {p.key for p in PROVIDERS}
    assert "openai" in keys and "deepseek" in keys and "ollama" in keys
    # 每个 provider 的 key_env 与 llm 映射一致
    for p in PROVIDERS:
        assert p.key_env == PROVIDER_ENV_MAP[p.key][0]


def test_build_env_updates_openai() -> None:
    from src.cli.onboard import build_env_updates

    upd = build_env_updates("openai", "gpt-4o-mini", "sk-abc", "")
    assert upd["LANGCHAIN_PROVIDER"] == "openai"
    assert upd["LANGCHAIN_MODEL_NAME"] == "gpt-4o-mini"
    assert upd["OPENAI_API_KEY"] == "sk-abc"


def test_build_env_updates_ollama_no_key() -> None:
    from src.cli.onboard import build_env_updates

    upd = build_env_updates("ollama", "llama3", "", "http://localhost:11434")
    assert "OLLAMA_BASE_URL" in upd
    assert not any(k.endswith("_API_KEY") for k in upd)


def test_merge_env_file_roundtrip(tmp_path) -> None:
    from src.cli.onboard import merge_env_file

    env = tmp_path / ".env"
    env.write_text("EXISTING=1\nLANGCHAIN_PROVIDER=old\n", encoding="utf-8")
    merge_env_file(env, {"LANGCHAIN_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x"})
    text = env.read_text(encoding="utf-8")
    assert "EXISTING=1" in text
    assert "LANGCHAIN_PROVIDER=openai" in text
    assert "LANGCHAIN_PROVIDER=old" not in text
    assert "OPENAI_API_KEY=sk-x" in text


def test_run_onboarding_writes_env(tmp_path, monkeypatch) -> None:
    from rich.console import Console
    from src.cli import onboard

    answers = iter(["1", "gpt-4o-mini", "sk-test", ""])  # provider#、model、key、base_url

    def fake_prompt(label, *, is_password=False):
        return next(answers)

    env = tmp_path / ".env"
    path = onboard.run_onboarding(Console(no_color=True), prompt_fn=fake_prompt, env_path=env)
    assert path == env
    text = env.read_text(encoding="utf-8")
    assert "LANGCHAIN_MODEL_NAME=gpt-4o-mini" in text
    assert "sk-test" in text
    import os
    assert os.environ.get("LANGCHAIN_MODEL_NAME") == "gpt-4o-mini"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_onboard.py -v`
Expected: FAIL（`No module named 'src.cli.onboard'`；以及 llm.PROVIDER_ENV_MAP 若 Step1 未做也会失败）

- [ ] **Step 4: 实现 `src/cli/onboard.py`**

```python
"""首次运行 onboarding 向导：选 provider → 填 model → 粘 key → 写 .env。

API key 只由用户键入并写入本地 .env（0600），绝不外传。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.providers.llm import PROVIDER_ENV_MAP

DEFAULT_ENV_PATH: Path = Path.home() / ".mini-agent" / ".env"

# provider -> 默认 model（补齐 PROVIDER_ENV_MAP 的展示信息）
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-1.5-flash",
    "groq": "llama-3.1-8b-instant",
    "dashscope": "qwen-plus",
    "qwen": "qwen-plus",
    "zhipu": "glm-4-flash",
    "moonshot": "moonshot-v1-8k",
    "minimax": "MiniMax-M3",
    "ollama": "llama3",
}


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    label: str
    key_env: Optional[str]
    base_env: str
    default_model: str


def _build_providers() -> tuple[ProviderInfo, ...]:
    infos: list[ProviderInfo] = []
    for key, (key_env, base_env) in PROVIDER_ENV_MAP.items():
        infos.append(ProviderInfo(
            key=key, label=key, key_env=key_env, base_env=base_env,
            default_model=_DEFAULT_MODELS.get(key, ""),
        ))
    return tuple(infos)


PROVIDERS: tuple[ProviderInfo, ...] = _build_providers()


def build_env_updates(provider: str, model: str, api_key: str,
                      base_url: str = "") -> dict[str, str]:
    """按 provider 生成要写入 .env 的键值。"""
    spec = PROVIDER_ENV_MAP.get(provider, PROVIDER_ENV_MAP["openai"])
    key_env, base_env = spec
    updates: dict[str, str] = {
        "LANGCHAIN_PROVIDER": provider,
        "LANGCHAIN_MODEL_NAME": model,
    }
    if key_env and api_key:
        updates[key_env] = api_key
    if base_url:
        updates[base_env] = base_url
    return updates


def merge_env_file(path: Path, updates: dict[str, str]) -> None:
    """把 updates 合并进 .env：已有键覆盖，其余追加。尽力 chmod 600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: dict[str, int] = {}
    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        seen[k] = i
    for key, value in updates.items():
        newline = f"{key}={value}"
        if key in seen:
            lines[seen[key]] = newline
        else:
            lines.append(newline)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def needs_onboarding() -> bool:
    """判断是否缺少可用配置（model 或对应 key 缺失）。"""
    from src.providers import llm as _llm

    _llm._ensure_dotenv()
    provider = os.getenv("LANGCHAIN_PROVIDER", "").strip().lower()
    model = os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    if not provider or not model:
        return True
    key_env, _ = PROVIDER_ENV_MAP.get(provider, PROVIDER_ENV_MAP["openai"])
    if key_env is None:  # ollama 无需 key
        return False
    return not (os.getenv(key_env) or os.getenv("OPENAI_API_KEY"))


def _default_prompt(label: str, *, is_password: bool = False) -> str:
    """默认交互读取。TTY 用 prompt_toolkit（key 掩码），否则退回 input。"""
    import sys

    if sys.stdin.isatty():
        try:
            from prompt_toolkit import prompt as pt_prompt

            return pt_prompt(label, is_password=is_password).strip()
        except Exception:  # noqa: BLE001
            pass
    return input(label).strip()


def run_onboarding(console, *, prompt_fn: Optional[Callable[..., str]] = None,
                   env_path: Optional[Path] = None) -> Path:
    """运行向导，写入 .env 并同步 os.environ；返回写入路径。"""
    from rich.text import Text

    ask = prompt_fn or _default_prompt
    path = env_path or DEFAULT_ENV_PATH

    console.print(Text("首次运行配置向导", style="bold"))
    console.print("可用 provider：")
    for i, p in enumerate(PROVIDERS, start=1):
        console.print(f"  {i:>2}. {p.label}")

    raw = ask("选择 provider 序号 [1]: ") or "1"
    try:
        idx = max(1, min(len(PROVIDERS), int(raw)))
    except ValueError:
        idx = 1
    provider = PROVIDERS[idx - 1]

    model = ask(f"model 名称 [{provider.default_model}]: ") or provider.default_model

    api_key = ""
    if provider.key_env is not None:
        api_key = ask(f"{provider.key_env}（粘贴你的 API key）: ", is_password=True)

    base_url = ask("base_url（可选，回车跳过）: ")

    updates = build_env_updates(provider.key, model, api_key, base_url)
    merge_env_file(path, updates)
    for k, v in updates.items():
        os.environ[k] = v
    console.print(Text(f"已写入 {path}", style="bold"))
    return path


__all__ = [
    "ProviderInfo", "PROVIDERS", "DEFAULT_ENV_PATH",
    "build_env_updates", "merge_env_file", "needs_onboarding", "run_onboarding",
]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_onboard.py tests/test_streaming.py -v`
Expected: PASS（onboard 5 passed；`test_streaming.py` 用于确认 llm.py 改动未破坏既有行为——若该文件不涉及 llm 可改跑 `uv run pytest -q` 全量）

- [ ] **Step 6: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/providers/llm.py src/cli/onboard.py tests/cli/test_onboard.py
git commit -m "feat(cli): 新增 onboard.py 首次运行向导 + 暴露 PROVIDER_ENV_MAP

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: input.py 交互输入（prompt_toolkit + 非 TTY 降级）

**Files:**
- Create: `REPO/src/cli/input.py`
- Test: `REPO/tests/cli/test_input.py`

**Interfaces:**
- Consumes: `src.cli.completer.SlashCompleter`、`src.cli.theme.Theme.brand_hex`
- Produces:
  - `src.cli.input.InteractiveInput`：
    - `__init__(self, *, provider: str, model: str, skills: int, history_path: Path | None = None)`
    - `read() -> str`（读取一行；EOF/Ctrl+D 抛 `EOFError`；Ctrl+C 抛 `KeyboardInterrupt`）
    - 属性 `is_tty: bool`

- [ ] **Step 1: 写失败测试 `tests/cli/test_input.py`**

```python
"""InteractiveInput 测试：非 TTY 降级路径。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def test_non_tty_falls_back_to_stdin(monkeypatch) -> None:
    from src.cli.input import InteractiveInput

    monkeypatch.setattr(sys, "stdin", io.StringIO("hello agent\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    inp = InteractiveInput(provider="openai", model="m", skills=3)
    assert inp.is_tty is False
    assert inp.read() == "hello agent"


def test_non_tty_eof_raises(monkeypatch) -> None:
    from src.cli.input import InteractiveInput

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    inp = InteractiveInput(provider="openai", model="m", skills=0)
    try:
        inp.read()
        assert False, "expected EOFError"
    except EOFError:
        pass
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_input.py -v`
Expected: FAIL（`No module named 'src.cli.input'`）

- [ ] **Step 3: 实现 `src/cli/input.py`**

```python
"""交互输入：TTY 用 prompt_toolkit（历史/补全/底部工具栏），否则退回 stdin。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_PROMPT = "mini-agent> "


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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_input.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/input.py tests/cli/test_input.py
git commit -m "feat(cli): 新增 input.py 交互输入（prompt_toolkit + 非 TTY 降级）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: app.py 前门（装配 + onboarding + REPL）

**Files:**
- Create: `REPO/src/cli/app.py`
- Test: `REPO/tests/cli/test_app.py`

**Interfaces:**
- Consumes: `src.cli.banner/stream/input/commands/onboard`、`src.cli.theme.get_console`；现有 `src.agent.loop.AgentLoop`、`src.agent.skills.SkillsLoader`、`src.agent.subagent.SubAgentContext`、`src.providers.chat.ChatLLM`、`src.memory.persistent.PersistentMemory`、`src.tools.build_registry`、`src.tools.delegate_tool.DelegateTool`、`src.tools.team_tool.TeamTool`
- Produces:
  - `src.cli.app.main() -> None`（进程入口）
  - `src.cli.app.run_repl(*, agent, skills, renderer, reader, console, history) -> None`（可测的 REPL 循环）
  - `src.cli.app._configure_stdio_utf8() -> None`

- [ ] **Step 1: 写失败测试 `tests/cli/test_app.py`**

```python
"""app.py REPL 循环测试（mock agent，不触网）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class _FakeReader:
    def __init__(self, lines):
        self._it = iter(lines)

    def read(self):
        try:
            return next(self._it)
        except StopIteration:
            raise EOFError


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run(self, *, user_message, history=None, **kw):
        self.calls.append(user_message)
        return {"status": "success", "content": f"echo:{user_message}", "run_dir": None}


def test_repl_runs_user_turn_then_quits() -> None:
    from rich.console import Console
    from src.cli.app import run_repl
    from src.cli.stream import StreamRenderer

    agent = _FakeAgent()
    reader = _FakeReader(["hello", "/quit"])
    console = Console(no_color=True, record=True)
    history: list[dict] = []

    run_repl(agent=agent, skills=[], renderer=StreamRenderer(console),
             reader=reader, console=console, history=history)

    assert agent.calls == ["hello"]
    assert any(m["content"] == "echo:hello" for m in history)


def test_repl_slash_help_does_not_call_agent() -> None:
    from rich.console import Console
    from src.cli.app import run_repl
    from src.cli.stream import StreamRenderer

    agent = _FakeAgent()
    reader = _FakeReader(["/help", "/quit"])
    console = Console(no_color=True, record=True)

    run_repl(agent=agent, skills=[], renderer=StreamRenderer(console),
             reader=reader, console=console, history=[])

    assert agent.calls == []
    assert "/help" in console.export_text()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_app.py -v`
Expected: FAIL（`No module named 'src.cli.app'`）

- [ ] **Step 3: 实现 `src/cli/app.py`**

```python
"""mini-agent 交互 CLI 前门：装配组件 → onboarding → banner → REPL。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2]
RUNS_DIR = AGENT_DIR / "runs"


def _configure_stdio_utf8() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 以免 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def run_repl(*, agent, skills, renderer, reader, console, history) -> None:
    """REPL 主循环。斜杠命令本地拦截；其余喂给 agent，用 renderer 流式展示。"""
    from rich.text import Text

    from src.cli.commands import handle_builtin_command

    while True:
        try:
            user_input = reader.read()
        except (EOFError, KeyboardInterrupt):
            console.print(Text("\nGoodbye!"))
            break

        if not user_input:
            continue

        cmd = handle_builtin_command(user_input, history=history, skills=skills)
        if cmd.handled:
            if cmd.clear_screen:
                console.clear()
            if cmd.clear_history:
                history.clear()
            if cmd.output:
                console.print(cmd.output)
            if cmd.quit:
                break
            continue

        renderer.begin()
        # AgentLoop 的 event_callback 已在 main() 构造时固定注入 renderer.handle，
        # 因此这里只传 user_message / history。
        result = agent.run(user_message=user_input, history=history or None)

        status = result.get("status", "unknown")
        content = result.get("content", "")
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})
        renderer.finish(status=status, content=content, run_dir=result.get("run_dir"))


def main() -> None:
    _configure_stdio_utf8()
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))

    from src.cli.banner import print_banner
    from src.cli.input import InteractiveInput
    from src.cli.onboard import needs_onboarding, run_onboarding
    from src.cli.stream import StreamRenderer
    from src.cli.theme import get_console

    console = get_console()

    if needs_onboarding():
        run_onboarding(console)

    from src.agent.loop import AgentLoop
    from src.agent.skills import SkillsLoader
    from src.agent.subagent import SubAgentContext
    from src.memory.persistent import PersistentMemory
    from src.providers.chat import ChatLLM
    from src.tools import build_registry
    from src.tools.delegate_tool import DelegateTool
    from src.tools.team_tool import TeamTool

    pm = PersistentMemory()
    llm = ChatLLM()
    registry = build_registry(persistent_memory=pm)
    skills = list(SkillsLoader().skills)

    parent_ctx = SubAgentContext(depth=0, parent_run_dir=RUNS_DIR, parent_session_id="cli")
    registry.register(DelegateTool(llm, registry, RUNS_DIR, parent_ctx))
    registry.register(TeamTool(llm, registry, RUNS_DIR, parent_ctx))

    renderer = StreamRenderer(console)
    agent = AgentLoop(
        registry=registry, llm=llm, max_iterations=50,
        persistent_memory=pm,
        event_callback=lambda et, data: renderer.handle(et, data),
    )

    provider = os.getenv("LANGCHAIN_PROVIDER", "default")
    model = llm.model_name or os.getenv("LANGCHAIN_MODEL_NAME", "default")

    try:
        version = _read_version()
    except Exception:  # noqa: BLE001
        version = "0.1.0"

    reader = InteractiveInput(provider=provider, model=model, skills=len(skills))
    if reader.is_tty:
        print_banner(console, provider=provider, model=model,
                     skills=len(skills), version=version)

    history: list[dict] = []
    run_repl(agent=agent, skills=skills, renderer=renderer,
             reader=reader, console=console, history=history)


def _read_version() -> str:
    try:
        from importlib.metadata import version as _v

        return _v("mini-agent")
    except Exception:  # noqa: BLE001
        return "0.1.0"


__all__ = ["main", "run_repl"]
```

> 说明：`AgentLoop` 在 `main()` 里通过构造参数 `event_callback` 固定注入 `renderer.handle`，因此 REPL 里 `agent.run` 只传 `user_message`/`history`。测试用的 `_FakeAgent.run(*, user_message, history=None, **kw)` 兼容该签名。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_app.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add src/cli/app.py tests/cli/test_app.py
git commit -m "feat(cli): 新增 app.py 前门（装配 + onboarding + REPL）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: 根 cli.py 薄 shim + __init__ 再导出 + pyproject 入口 + 迁移旧测试

**Files:**
- Modify: `REPO/cli.py`（整文件替换为 shim）
- Modify: `REPO/src/cli/__init__.py`（补 main + 再导出）
- Modify: `REPO/pyproject.toml`（`[project.scripts]` 新增 `mini-agent`）
- Modify: `REPO/tests/test_cli_terminal_ui.py`（迁移 banner 断言）
- Test（回归）: `REPO/tests/test_cli_cache_line.py`（不改，验证 `cli.format_cache_stats_line` 仍可用）

**Interfaces:**
- Consumes: `src.cli.app.main`、`src.cli.stream.format_cache_stats_line`、`src.cli.commands.*`
- Produces: 顶层 `cli` 模块暴露 `main`、`format_cache_stats_line`、`handle_builtin_command`、`CommandResult`、`format_help`、`format_history_summary`、`format_skills_summary`

- [ ] **Step 1: 补 `src/cli/__init__.py` 再导出**

整文件替换为：
```python
"""mini-agent 交互 CLI 包。UI 全部在此，根 cli.py 仅为薄 shim。"""
from __future__ import annotations

from src.cli.app import main
from src.cli.commands import (
    CommandResult,
    format_help,
    format_history_summary,
    format_skills_summary,
    handle_builtin_command,
)
from src.cli.stream import format_cache_stats_line

__all__ = [
    "main",
    "format_cache_stats_line",
    "handle_builtin_command",
    "CommandResult",
    "format_help",
    "format_history_summary",
    "format_skills_summary",
]
```

- [ ] **Step 2: 整文件替换根 `cli.py` 为 shim**

```python
#!/usr/bin/env python3
"""mini-agent 交互 CLI —— 薄入口 shim。真实实现见 src/cli/。

保留 `python cli.py` 用法，并向后兼容再导出旧公共符号
（format_cache_stats_line / handle_builtin_command / ...）。
"""

from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from src.cli.app import main  # noqa: E402
from src.cli.commands import (  # noqa: E402,F401
    CommandResult,
    format_help,
    format_history_summary,
    format_skills_summary,
    handle_builtin_command,
)
from src.cli.stream import format_cache_stats_line  # noqa: E402,F401

__all__ = [
    "main",
    "format_cache_stats_line",
    "handle_builtin_command",
    "CommandResult",
    "format_help",
    "format_history_summary",
    "format_skills_summary",
]

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: pyproject 新增 `mini-agent` 入口**

在 `[project.scripts]` 段（当前为 `mini-agent-mcp` / `mini-agent-gateway`）**顶部**加一行：
```toml
[project.scripts]
mini-agent = "cli:main"
mini-agent-mcp = "mcp_server:main"
mini-agent-gateway = "gateway:main"
```

- [ ] **Step 4: 迁移旧 banner 测试 `tests/test_cli_terminal_ui.py`**

把 `test_banner_shows_runtime_context` 整个函数替换为（其余函数保持不变）：
```python
def test_banner_shows_runtime_context() -> None:
    from rich.console import Console

    from src.cli.banner import print_banner

    con = Console(record=True, no_color=True, width=100)
    print_banner(con, provider="openai", model="gpt-test", skills=12, version="0.1.0")
    out = con.export_text()

    assert "mini-agent" in out.lower()
    assert "openai" in out
    assert "gpt-test" in out
    assert "skills:12" in out
```

其余三个测试（`test_help_lists_terminal_commands`、`test_history_summary_limits_recent_turns`、
`test_command_dispatch_handles_builtin_commands`）无需改动——它们调用的 `cli.format_help` /
`cli.format_history_summary` / `cli.handle_builtin_command` 已由 shim 再导出。

- [ ] **Step 5: 运行回归 + 迁移测试确认通过**

Run: `uv run pytest tests/test_cli_terminal_ui.py tests/test_cli_cache_line.py -v`
Expected: PASS（旧套件全绿：banner 迁移版通过；cache line 通过；help/history/dispatch 通过）

- [ ] **Step 6: 冒烟验证入口可解析**

Run:
```bash
cd "E:/03_个人项目归档/mini-agent" && uv run python -c "import cli; print(callable(cli.main), callable(cli.format_cache_stats_line))"
```
Expected: `True True`

- [ ] **Step 7: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add cli.py src/cli/__init__.py pyproject.toml tests/test_cli_terminal_ui.py
git commit -m "feat(cli): 根 cli.py 改薄 shim + mini-agent 入口 + 迁移旧测试

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: 全量回归 + README/文档 uv 优先重写

**Files:**
- Modify: `REPO/README.md`（Quick Start 段）
- Modify: `REPO/docs/learning/06-entrypoints.md`（入口未注册遗留说明）
- Test（全量）: 整个 `tests/`

**Interfaces:**
- Consumes: 无
- Produces: 无（文档 + 验收）

- [ ] **Step 1: 全量测试回归**

Run: `uv run pytest tests/ -q`
Expected: 全绿（新旧全部通过）。若个别既有非 CLI 测试需要网络而失败，记录但不阻塞本任务（与本次改动无关）。

- [ ] **Step 2: ruff 静态检查**

Run: `cd "E:/03_个人项目归档/mini-agent" && uv run ruff check src/cli cli.py`
Expected: `All checks passed!`（如有告警就地修复）

- [ ] **Step 3: 重写 README Quick Start**

将 `README.md` 中 `## Quick Start` 段落里的安装与运行部分替换为：
````markdown
## Quick Start

```bash
# 1. 安装依赖（uv 优先）
uv sync

# 2. 首次运行 —— 进入配置向导（选 provider / 填 model / 粘 API key，自动写 ~/.mini-agent/.env）
uv run mini-agent

# —— 或者装成全局命令 ——
uv tool install .
mini-agent

# 3a. MCP server（Claude Desktop / Cursor 等）
uv run mini-agent-mcp

# 3b. MCP server（SSE）
uv run mini-agent-mcp --transport sse --port 8900

# 3c. IM 网关（企业微信 / 个人微信）
uv sync --extra gateway
uv run mini-agent-gateway init
uv run mini-agent-gateway doctor
uv run mini-agent-gateway run
```

> 免安装直跑（开发调试）：`python cli.py` 仍可用。
````

- [ ] **Step 4: 更新 `docs/learning/06-entrypoints.md`**

在 §6.3 `entry_points` 小节，把"注意 **CLI 入口没有注册** …… （这是个小遗留点）"一段替换为：
```markdown
`[project.scripts]` 现在注册了三个命令：

```toml
[project.scripts]
mini-agent = "cli:main"          # 交互 CLI（薄 shim → src/cli/app.py）
mini-agent-mcp = "mcp_server:main"
mini-agent-gateway = "gateway:main"
```

`mini-agent` 指向根 `cli.py` 的 `main`，而 `cli.py` 已改为薄 shim，真实交互实现在
`src/cli/`（theme/banner/stream/input/completer/commands/onboard/app）。因此
`uv tool install .` 后直接 `mini-agent` 即可启动美化终端，`python cli.py` 也仍可用。
```

- [ ] **Step 5: 手动冒烟（非 TTY 降级）**

Run:
```bash
cd "E:/03_个人项目归档/mini-agent" && echo "/quit" | uv run mini-agent
```
Expected: 不抛异常，正常退出（非 TTY 走 stdin 降级路径；若 `needs_onboarding()` 为真会先进向导——本冒烟应在已配置 `.env` 环境下执行；未配置时改跑 `printf '1\ngpt-4o-mini\nsk-x\n\n/quit\n' | uv run mini-agent` 走完向导再退出）。

- [ ] **Step 6: 提交**

```bash
cd "E:/03_个人项目归档/mini-agent"
git add README.md docs/learning/06-entrypoints.md
git commit -m "docs: README/入口文档改为 uv 优先，登记 mini-agent 命令

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 自审（Self-Review）

**1. Spec 覆盖检查**

| Spec 要求 | 对应 Task |
|-----------|-----------|
| src/cli/ 包分层 + 根 shim | Task 1–10 |
| theme.py 紫色主题 + 深浅色 + NO_COLOR | Task 1 |
| stream.py spinner + 工具行 + Markdown + cache 行 | Task 2、3 |
| commands.py 5 命令 + 向后兼容 | Task 4、10 |
| completer.py 斜杠补全 | Task 5 |
| banner.py 渐变字标 | Task 6 |
| onboard.py 向导 + 写 ~/.mini-agent/.env + PROVIDER_ENV_MAP 复用 | Task 7 |
| input.py prompt_toolkit + 非 TTY 降级 | Task 8 |
| app.py 装配 + onboarding gate + REPL + 事件接线 | Task 9 |
| pyproject `mini-agent=cli:main` + rich/prompt_toolkit 依赖 | Task 1、5、10 |
| README uv 优先 + 06-entrypoints 更新 | Task 11 |
| 向后兼容旧测试 | Task 10 |
| 全量回归 + ruff | Task 11 |

无遗漏。

**2. 占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码。

**3. 类型/签名一致性**：
- `format_cache_stats_line` 签名/行为在 Task 2 定义，Task 3/10 复用一致。
- `CommandResult` 字段（含新增 `quit`）在 Task 4 定义，Task 9 REPL 用 `cmd.quit` 一致。
- `StreamRenderer.begin/handle/finish` 在 Task 3 定义，Task 9 调用一致。
- `InteractiveInput.read/is_tty` 在 Task 8 定义，Task 9 使用一致。
- `PROVIDER_ENV_MAP` 在 Task 7 提升，onboard 消费一致。
- `print_banner(console, *, provider, model, skills, version)` 在 Task 6 定义，Task 9/10 调用一致。

**4. 歧义**：onboarding 写入位置固定 `~/.mini-agent/.env`（`_ENV_CANDIDATES[0]`，全局命令生效）；`needs_onboarding` 复用 llm 的 env 解析，判据明确。
