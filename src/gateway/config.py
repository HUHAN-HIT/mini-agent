"""Gateway YAML config loader with ${VAR} and ~/ expansion.

Config is the single source of truth for the runner. We expand:

- ``${VAR}`` / ``${VAR:-default}`` from process environment.
- ``~`` and ``~user`` to home directories via ``os.path.expanduser``.
- ``${VAR:-}`` is the explicit "may be empty" form for optional platform
  credentials.

We deliberately don't pull in a full YAML lib with include support; the gateway
config is small and self-contained.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError("pyyaml is required for gateway config (pip install pyyaml)") from exc

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _replace_env(match: re.Match) -> str:
    name = match.group(1)
    default = match.group(2) or ""
    return os.environ.get(name, default)


def expand_path(path: str | Path) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(path)))).resolve(strict=False)


def _expand_paths(value: Any, path_keys: set[str]) -> Any:
    if isinstance(value, dict):
        expanded: dict[str, Any] = {}
        for key, child in value.items():
            if key in path_keys and isinstance(child, str):
                expanded[key] = str(expand_path(child))
            else:
                expanded[key] = _expand_paths(child, path_keys)
        return expanded
    if isinstance(value, list):
        return [_expand_paths(item, path_keys) for item in value]
    return value


_PATH_KEYS: set[str] = {
    "data_dir",
    "dir",
    "router_path",
    "file",
    "log_file",
    "status_file",
    "hermes_home",
    "hermes_lock_dir",
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load and fully expand gateway config.

    Falls back to a sensible default if no path is given. The default only
    enables wecom with empty credentials; the caller (doctor/runner) is
    expected to surface missing values.
    """

    if path is None:
        path = Path("gateway.yaml")
    path = expand_path(path)

    if not path.exists():
        if path.name == "gateway.yaml" and path.parent == Path.cwd():
            return default_config()
        raise FileNotFoundError(f"gateway config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    expanded = _expand_env(raw)
    expanded = _expand_paths(expanded, _PATH_KEYS)
    if not isinstance(expanded, dict):
        raise ValueError(f"gateway config root must be a mapping: {path}")
    return expanded


def default_config() -> dict[str, Any]:
    """Default config skeleton (no credentials, all platforms disabled)."""

    return {
        "server": {"host": "0.0.0.0", "port": 8645},
        "data_dir": str(expand_path("~/.mini-agent/gateway")),
        "locks": {
            "enabled": True,
            "dir": str(expand_path("~/.mini-agent/gateway/locks")),
            "stale_after_seconds": 86400,
            "check_hermes": False,
            "hermes_home": "",
            "hermes_lock_dir": "",
        },
        "session": {
            "router_path": str(expand_path("~/.mini-agent/gateway/sessions_map.json")),
            "group_sessions_per_user": True,
            "thread_sessions_per_user": False,
            "per_session_serial": True,
            "history_max_chars": 12000,
        },
        "platforms": {
            "wecom": {"enabled": False, "apps": []},
            "weixin": {"enabled": False},
        },
        "logging": {
            "level": "INFO",
            "file": str(expand_path("~/.mini-agent/gateway/gateway.log")),
        },
        "service": {
            "name": "mini-agent-gateway",
            "autostart": False,
            "start_on": "logon",
            "python": ".venv/Scripts/python.exe",
            "cwd": ".",
            "args": ["gateway.py", "run", "--config", "gateway.yaml"],
            "log_file": str(expand_path("~/.mini-agent/gateway/logs/service.log")),
            "status_file": str(expand_path("~/.mini-agent/gateway/status.json")),
            "restart": {"enabled": True, "max_attempts": 5, "delay_seconds": 10},
        },
    }
