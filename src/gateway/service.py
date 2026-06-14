"""Service install/start/stop/status for the gateway daemon.

P0 targets Windows user-level Task Scheduler because:

- No admin elevation required.
- Supports working directory + arbitrary command line.
- Runs after user logon, which is the normal case for desktop WeCom/WeChat
  integrations.

Linux/macOS path is left abstract via ``ServiceManager``; concrete
implementations can be added in P4.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.gateway.config import expand_path
from src.gateway.router import atomic_write_json, load_json
from src.gateway.status import mark_stopped, read_status, write_status

logger = logging.getLogger(__name__)


@dataclass
class ServiceSpec:
    name: str
    python: str
    cwd: str
    args: list[str]
    log_file: str
    status_file: str
    config_path: str = ""
    start_on: str = "logon"  # logon | boot (boot needs admin)
    autostart: bool = False
    restart: dict = field(default_factory=lambda: {"enabled": True, "max_attempts": 5, "delay_seconds": 10})

    def command_line(self) -> str:
        parts = [self.python] + list(self.args)
        return " ".join(_quote(p) for p in parts)


def _quote(value: str) -> str:
    if not value or any(c in value for c in " \t"):
        return f'"{value}"'
    return value


@dataclass
class ServiceStatus:
    installed: bool
    running: bool
    pid: Optional[int]
    state: str
    last_error: Optional[str]
    enabled_platforms: list[str]
    config_path: Optional[str]
    log_path: Optional[str]
    host: Optional[str]
    port: Optional[int]
    raw: dict = field(default_factory=dict)


class ServiceManager(ABC):
    """Abstract service backend. Concrete impls: Windows Task Scheduler."""

    @abstractmethod
    def install(self, spec: ServiceSpec) -> None: ...

    @abstractmethod
    def uninstall(self, name: str) -> None: ...

    @abstractmethod
    def start(self, name: str) -> None: ...

    @abstractmethod
    def stop(self, name: str) -> None: ...

    @abstractmethod
    def is_installed(self, name: str) -> bool: ...

    @abstractmethod
    def is_running(self, name: str) -> bool: ...


class WindowsTaskSchedulerManager(ServiceManager):
    """Windows Task Scheduler user-task backend (no admin needed)."""

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("WindowsTaskSchedulerManager only supported on Windows")

    def _ps(self, args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command",
            "; ".join(args),
        ]
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
        )

    def is_installed(self, name: str) -> bool:
        res = self._ps([f"Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue | Out-String"])
        return name in (res.stdout or "")

    def is_running(self, name: str) -> bool:
        res = self._ps([
            f"(Get-ScheduledTaskInfo -TaskName '{name}' -ErrorAction SilentlyContinue).LastTaskResult",
            "Write-Output '---'",
            f"$task = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue",
            "if ($task) { $task.State }",
        ])
        out = (res.stdout or "").strip()
        return "Running" in out

    def install(self, spec: ServiceSpec) -> None:
        action = (
            f"New-ScheduledTaskAction -Execute '{spec.python}' "
            f"-Argument '{' '.join(_quote(a) for a in spec.args)}' "
            f"-WorkingDirectory '{spec.cwd}'"
        )
        if spec.start_on == "logon":
            trigger = "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME"
        else:
            trigger = "New-ScheduledTaskTrigger -AtStartup"

        restart_cfg = spec.restart or {}
        settings_parts = [
            "New-ScheduledTaskSettingsSet",
            "-AllowStartIfOnBatteries",
            "-DontStopIfGoingOnBatteries",
            "-StartWhenAvailable",
        ]
        if restart_cfg.get("enabled"):
            attempts = int(restart_cfg.get("max_attempts", 5))
            delay = int(restart_cfg.get("delay_seconds", 10))
            settings_parts.append(f"-RestartCount {attempts}")
            settings_parts.append(f"-RestartInterval (New-TimeSpan -Seconds {delay})")

        settings = " ".join(settings_parts)
        principal = (
            "New-ScheduledTaskPrincipal -UserId $env:USERNAME "
            "-LogonType Interactive -RunLevel Limited"
        )

        register = (
            f"Register-ScheduledTask -TaskName '{spec.name}' "
            f"-Action $action -Trigger $trigger -Settings $settings "
            f"-Principal $principal -Force"
        )

        script = [
            "$ErrorActionPreference = 'Stop'",
            f"$action = {action}",
            f"$trigger = {trigger}",
            f"$settings = {settings}",
            f"$principal = {principal}",
            register,
        ]
        res = self._ps(script)
        if res.returncode != 0:
            raise RuntimeError(f"Register-ScheduledTask failed: {res.stderr.strip() or res.stdout.strip()}")

    def uninstall(self, name: str) -> None:
        res = self._ps([f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction SilentlyContinue"])
        if res.returncode != 0 and "cannot find" not in (res.stderr or "").lower():
            raise RuntimeError(f"Unregister-ScheduledTask failed: {res.stderr.strip()}")

    def start(self, name: str) -> None:
        res = self._ps([f"Start-ScheduledTask -TaskName '{name}'"])
        if res.returncode != 0:
            raise RuntimeError(f"Start-ScheduledTask failed: {res.stderr.strip()}")

    def stop(self, name: str) -> None:
        res = self._ps([f"Stop-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue"])
        if res.returncode != 0:
            raise RuntimeError(f"Stop-ScheduledTask failed: {res.stderr.strip()}")


def make_service_manager() -> ServiceManager:
    if sys.platform.startswith("win"):
        return WindowsTaskSchedulerManager()
    raise RuntimeError(f"no service manager for platform {sys.platform}")


def spec_from_config(config: dict, config_path: Optional[Path] = None) -> ServiceSpec:
    service_cfg = config.get("service") or {}
    name = service_cfg.get("name") or "mini-agent-gateway"
    python = service_cfg.get("python") or sys.executable
    cwd = service_cfg.get("cwd") or "."
    args = list(service_cfg.get("args") or ["gateway.py", "run"])
    log_file = service_cfg.get("log_file") or "~/.mini-agent/gateway/logs/service.log"
    status_file = service_cfg.get("status_file") or "~/.mini-agent/gateway/status.json"
    return ServiceSpec(
        name=name,
        python=str(expand_path(python)),
        cwd=str(expand_path(cwd)),
        args=args,
        log_file=str(expand_path(log_file)),
        status_file=str(expand_path(status_file)),
        config_path=str(config_path) if config_path else "",
        start_on=service_cfg.get("start_on", "logon"),
        autostart=bool(service_cfg.get("autostart", False)),
        restart=service_cfg.get("restart") or {"enabled": True, "max_attempts": 5, "delay_seconds": 10},
    )


def status(name: str, status_file: Path) -> ServiceStatus:
    """Combine OS service state with the runtime status file."""

    try:
        mgr = make_service_manager()
        installed = mgr.is_installed(name)
        running = mgr.is_running(name)
    except RuntimeError:
        mgr = None
        installed = False
        running = False

    status_data = read_status(status_file) or {}
    return ServiceStatus(
        installed=installed,
        running=running,
        pid=status_data.get("pid"),
        state=status_data.get("state", "unknown"),
        last_error=status_data.get("last_error"),
        enabled_platforms=list(status_data.get("enabled_platforms") or []),
        config_path=status_data.get("config_path"),
        log_path=status_data.get("log_path"),
        host=status_data.get("host"),
        port=status_data.get("port"),
        raw=status_data,
    )
