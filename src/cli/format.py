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
