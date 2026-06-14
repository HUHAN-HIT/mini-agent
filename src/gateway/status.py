"""Runtime status file writer/reader.

The service wrapper writes ``status.json`` on start/stop so ``gateway.py
service status`` can report host/port/pid/last_error without polling the OS
service manager. Status is informational — the service manager's view is the
source of truth for "is it running".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.gateway.router import atomic_write_json, load_json


def write_status(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def read_status(path: Path) -> Optional[dict[str, Any]]:
    data = load_json(path, default=None)
    return data if isinstance(data, dict) else None


def make_running_status(
    *,
    service_name: str,
    config_path: str,
    cwd: str,
    python: str,
    host: str,
    port: int,
    enabled_platforms: list[str],
    log_path: str,
) -> dict[str, Any]:
    return {
        "service_name": service_name,
        "state": "running",
        "pid": __import__("os").getpid(),
        "started_at": datetime.now().isoformat(),
        "config_path": config_path,
        "cwd": cwd,
        "python": python,
        "host": host,
        "port": port,
        "enabled_platforms": enabled_platforms,
        "log_path": log_path,
        "last_error": None,
    }


def mark_stopped(path: Path, *, last_error: Optional[str] = None) -> None:
    existing = read_status(path) or {}
    existing.update({
        "state": "stopped",
        "stopped_at": datetime.now().isoformat(),
        "last_error": last_error,
    })
    atomic_write_json(path, existing)
