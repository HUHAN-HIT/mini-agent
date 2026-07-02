"""Generate gateway.yaml from environment variables.

This is the auto-config path: the user fills a single ``.env`` file and runs
``python gateway.py init``. We inspect the environment, decide which platforms
have complete credentials, and emit a ``gateway.yaml`` that is structurally
identical to ``gateway.yaml.example`` (so the rest of the pipeline -- doctor,
runner -- works unchanged).

Design notes:

- A platform is enabled **iff all of its required credentials are present and
  non-empty** in the environment. Missing creds -> ``enabled: false`` (doctor
  will still surface the gap if the user expected it on).
- List-type fields (allow_from) come from comma-separated env vars.
- Everything here is pure: it reads a ``Mapping`` and returns a ``dict`` / str.
  File IO and ``.env`` loading live in the CLI (``gateway.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping


def _default_python() -> str:
    """Prefer a project-local venv if it actually exists; else the running
    interpreter. Hard-coding ``.venv/Scripts/python.exe`` breaks service
    install when the user never set up a venv (the venv python lacks the
    project's deps and exits 1 immediately)."""
    venv_py = Path(".venv/Scripts/python.exe") if sys.platform.startswith("win") else Path(".venv/bin/python")
    if venv_py.exists():
        return str(venv_py).replace("\\", "/")
    return sys.executable.replace("\\", "/")

try:
    import yaml  # type: ignore
except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError("pyyaml is required for gateway init (pip install pyyaml)") from exc

# Credentials that must all be present for a platform to auto-enable.
WECOM_REQUIRED = ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY")
WEIXIN_REQUIRED = ("WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN")

_DEFAULT_DATA_DIR = "~/.mini-agent/gateway"


def _val(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or "").strip() or default


def _csv(env: Mapping[str, str], key: str) -> list[str]:
    """Parse a comma-separated env var into a clean list (empty -> [])."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _all_present(env: Mapping[str, str], keys: tuple[str, ...]) -> bool:
    return all((env.get(k) or "").strip() for k in keys)


def _sub(data_dir: str, *parts: str) -> str:
    """Join sub-paths under data_dir using POSIX separators.

    Kept POSIX-style so the emitted YAML stays clean on Windows too; the loader
    expands ~ and resolves the path at runtime.
    """
    base = data_dir.rstrip("/").rstrip("\\")
    return "/".join([base, *parts])


def build_config_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Build a full gateway config dict from environment variables."""

    data_dir = _val(env, "GATEWAY_DATA_DIR", _DEFAULT_DATA_DIR)
    host = _val(env, "GATEWAY_HOST", "0.0.0.0")
    port = int(_val(env, "GATEWAY_PORT", "8645"))

    wecom_enabled = _all_present(env, WECOM_REQUIRED)
    weixin_enabled = _all_present(env, WEIXIN_REQUIRED)

    wecom_apps: list[dict[str, Any]] = []
    if wecom_enabled:
        wecom_apps = [
            {
                "name": "default",
                "corp_id": _val(env, "WECOM_CORP_ID"),
                "agent_id": _val(env, "WECOM_AGENT_ID"),
                "corp_secret": _val(env, "WECOM_SECRET"),
                "token": _val(env, "WECOM_TOKEN"),
                "encoding_aes_key": _val(env, "WECOM_AES_KEY"),
                "allow_from": _csv(env, "WECOM_ALLOW_FROM"),
            }
        ]

    config: dict[str, Any] = {
        "server": {"host": host, "port": port},
        "data_dir": data_dir,
        "locks": {
            "enabled": True,
            "dir": _sub(data_dir, "locks"),
            "stale_after_seconds": 86400,
            "check_hermes": False,
            "hermes_home": "",
            "hermes_lock_dir": "",
        },
        "session": {
            "router_path": _sub(data_dir, "sessions_map.json"),
            "group_sessions_per_user": True,
            "thread_sessions_per_user": False,
            "per_session_serial": True,
            "history_max_chars": 12000,
        },
        "platforms": {
            "wecom": {
                "enabled": wecom_enabled,
                "message_dedup_ttl_seconds": 300,
                "apps": wecom_apps,
            },
            "weixin": {
                "enabled": weixin_enabled,
                "bot_type": "3",
                "base_url": "https://ilinkai.weixin.qq.com",
                "cdn_base_url": "https://novac2c.c2c.weixin.qq.com/c2c",
                "account_id": _val(env, "WEIXIN_ACCOUNT_ID"),
                "bot_token": _val(env, "WEIXIN_TOKEN"),
                "dm_policy": _val(env, "WEIXIN_DM_POLICY", "allowlist"),
                "allow_from": _csv(env, "WEIXIN_ALLOW_FROM"),
                "group_policy": _val(env, "WEIXIN_GROUP_POLICY", "disabled"),
                "group_allow_from": _csv(env, "WEIXIN_GROUP_ALLOW_FROM"),
                "text_batch_delay_seconds": 3.0,
                "text_batch_split_delay_seconds": 5.0,
                "send_chunk_delay_seconds": 1.5,
                "rate_limit_circuit_threshold": 1,
                "rate_limit_circuit_window_seconds": 30,
                "rate_limit_circuit_open_seconds": 30,
            },
        },
        "logging": {
            "level": _val(env, "GATEWAY_LOG_LEVEL", "INFO"),
            "file": _sub(data_dir, "gateway.log"),
        },
        "service": {
            "name": "mini-agent-gateway",
            "autostart": False,
            "start_on": "logon",
            "python": _default_python(),
            "cwd": ".",
            "args": ["gateway.py", "run", "--config", "gateway.yaml"],
            "log_file": _sub(data_dir, "logs", "service.log"),
            "status_file": _sub(data_dir, "status.json"),
            "restart": {"enabled": True, "max_attempts": 5, "delay_seconds": 60},
        },
    }
    return config


def enabled_platforms(config: Mapping[str, Any]) -> list[str]:
    """Names of platforms marked enabled in a config dict."""
    platforms = config.get("platforms") or {}
    return [
        name
        for name, p in platforms.items()
        if isinstance(p, dict) and p.get("enabled")
    ]


def render_yaml(config: Mapping[str, Any]) -> str:
    """Render a config dict to YAML text (stable key order, unicode-friendly)."""
    header = (
        "# Generated by `python gateway.py init` from environment / .env.\n"
        "# Re-run init to regenerate; edit by hand only for advanced cases\n"
        "# (multi-app wecom, custom rate limits).\n"
    )
    body = yaml.safe_dump(
        dict(config),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return header + body
