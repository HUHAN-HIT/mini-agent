"""Path safety helpers used by file-access tools."""

from __future__ import annotations

from pathlib import Path


def _rejects_unc(p: str) -> None:
    if p.startswith("\\\\") or p.startswith("//"):
        raise ValueError(f"UNC paths are not allowed: {p!r}")


def safe_path(p: str, workdir: Path) -> Path:
    _rejects_unc(p)
    base = Path(workdir).resolve()
    resolved = (base / p).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path {p!r} escapes workspace {base}") from exc
    return resolved
