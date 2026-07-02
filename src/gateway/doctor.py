"""Doctor: validate config + environment before registering or starting gateway.

Each check returns a ``CheckResult``. ``fatal`` results block ``service
install``; ``warning`` results surface to the user but don't block. The CLI
prints both human-readable and ``--json`` output.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.gateway.adapters import _weixin_credentials_for_lock
from src.gateway.config import expand_path, load_config
from src.gateway.locks import PlatformLockManager, lock_identity_for_adapter

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str = "info"  # info | warning | fatal
    message: str = ""
    detail: Any = None


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def fatal_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == "fatal" and not c.ok)

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == "warning" and not c.ok)

    @property
    def ok(self) -> bool:
        return self.fatal_count == 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "fatal_count": self.fatal_count, "warning_count": self.warning_count,
                "checks": [asdict(c) for c in self.checks]}


def _check_python(report: DoctorReport) -> None:
    py = sys.executable
    if py and Path(py).exists():
        report.checks.append(CheckResult("python_executable", True, "info", py))
    else:
        report.checks.append(CheckResult("python_executable", False, "fatal", f"bad python path: {py}"))


def _check_cwd(report: DoctorReport) -> None:
    cwd = Path.cwd()
    report.checks.append(CheckResult("cwd", True, "info", str(cwd)))


def _check_data_dir(report: DoctorReport, data_dir: Path) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".write_test").write_text("ok", encoding="utf-8")
        (data_dir / ".write_test").unlink()
        report.checks.append(CheckResult("data_dir", True, "info", str(data_dir)))
    except OSError as exc:
        report.checks.append(CheckResult("data_dir", False, "fatal", str(exc), str(data_dir)))


def _check_port_free(report: DoctorReport, host: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.bind((host, port))
        report.checks.append(CheckResult("port_free", True, "info", f"{host}:{port}"))
    except OSError as exc:
        report.checks.append(CheckResult("port_free", False, "fatal", f"port {port} occupied: {exc}"))


def _check_logging(report: DoctorReport, log_file: Optional[Path]) -> None:
    if not log_file:
        report.checks.append(CheckResult("log_file", True, "info", "(stderr)"))
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8"):
            pass
        report.checks.append(CheckResult("log_file", True, "info", str(log_file)))
    except OSError as exc:
        report.checks.append(CheckResult("log_file", False, "fatal", str(exc)))


def _check_wecom(report: DoctorReport, wecom_cfg: dict) -> None:
    if not wecom_cfg:
        report.checks.append(CheckResult("wecom", True, "info", "not configured"))
        return
    if not wecom_cfg.get("enabled"):
        report.checks.append(CheckResult("wecom", True, "info", "disabled"))
        return
    apps = wecom_cfg.get("apps") or []
    if not apps:
        report.checks.append(CheckResult("wecom_apps", False, "fatal", "no apps configured"))
        return
    for app in apps:
        missing = [
            f for f in ("corp_id", "agent_id", "corp_secret", "token", "encoding_aes_key")
            if not str(app.get(f) or "").strip()
        ]
        if missing:
            report.checks.append(CheckResult(
                f"wecom_app_{app.get('name', '?')}",
                False,
                "fatal",
                f"missing fields: {missing}",
            ))
        else:
            report.checks.append(CheckResult(
                f"wecom_app_{app.get('name', '?')}",
                True,
                "info",
                f"corp={app['corp_id']} agent={app['agent_id']}",
            ))


def _check_weixin(report: DoctorReport, weixin_cfg: dict, data_dir: Path) -> None:
    if not weixin_cfg or not weixin_cfg.get("enabled"):
        report.checks.append(CheckResult("weixin", True, "info", "disabled"))
        return
    # Credentials may come from inline config OR the scan-login file. The file
    # is the source of truth after `gateway.py login weixin`: the scan flow
    # leaves inline ${WEIXIN_TOKEN:-} empty and relies on the persisted creds,
    # which is exactly what the adapter's connect() reads first.
    has_token = bool(str(weixin_cfg.get("bot_token") or "").strip())
    has_account = bool(str(weixin_cfg.get("account_id") or "").strip())
    source = "config"
    if not (has_token and has_account):
        creds_path = data_dir / "weixin_credentials.json"
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if str(data.get("bot_token") or "").strip() and str(data.get("account_id") or "").strip():
            has_token = has_account = True
            source = "scan-login file"
    if has_token and has_account:
        report.checks.append(
            CheckResult("weixin_credentials", True, "info", f"credentials present ({source})")
        )
    else:
        report.checks.append(CheckResult(
            "weixin_credentials",
            False,
            "warning",
            "no bot_token/account_id — run `gateway.py login weixin` before service install",
        ))


def _check_platform_locks(
    report: DoctorReport,
    config: dict,
    lock_manager: PlatformLockManager,
    data_dir: Optional[Path] = None,
) -> None:
    """Verify each enabled platform's resource lock is obtainable.

    A lock left behind by a crashed process used to block ``service install``
    with a FATAL even though the PID was dead. We now auto-clean stale locks
    (PID dead, or held past ``stale_after_seconds``) and surface them as a
    warning — only a *live* holder is FATAL, since stealing that would risk
    double-polling."""
    platforms = config.get("platforms") or {}

    def _record(name: str, scope: str, identity: str) -> None:
        info = lock_manager.describe(scope=scope, identity=identity)
        if info is None:
            report.checks.append(CheckResult(name, True, "info", f"{scope}/{identity} free"))
            return
        cleaned = lock_manager.cleanup_if_stale(scope=scope, identity=identity)
        if cleaned is not None:
            report.checks.append(CheckResult(
                name,
                True,
                "warning",
                f"removed stale {scope}/{identity} (pid={cleaned.pid} dead since {cleaned.started_at})",
            ))
        else:
            report.checks.append(CheckResult(
                name,
                False,
                "fatal",
                f"held by {info.owner} pid={info.pid} since {info.started_at}",
            ))

    if (platforms.get("wecom") or {}).get("enabled"):
        for app in (platforms["wecom"].get("apps") or []):
            scope, identity = lock_identity_for_adapter(
                platform="wecom",
                corp_id=app.get("corp_id", ""),
                agent_id=str(app.get("agent_id") or ""),
            )
            _record(f"lock_wecom_{app.get('name', '?')}", scope, identity)
    if (platforms.get("weixin") or {}).get("enabled"):
        _account_id, bot_token = _weixin_credentials_for_lock(platforms["weixin"], data_dir)
        scope, identity = lock_identity_for_adapter(platform="weixin", bot_token=bot_token)
        _record("lock_weixin", scope, identity)


def _check_task_scheduler(report: DoctorReport) -> None:
    if not sys.platform.startswith("win"):
        report.checks.append(CheckResult("task_scheduler", True, "info", "(non-Windows)"))
        return
    schtasks = Path("C:/Windows/System32/schtasks.exe")
    if schtasks.exists():
        report.checks.append(CheckResult("task_scheduler", True, "info", str(schtasks)))
    else:
        report.checks.append(CheckResult("task_scheduler", False, "fatal", "schtasks.exe not found"))


def run_doctor(config_path: Optional[Path] = None) -> DoctorReport:
    """Run all checks against the given config (or default)."""

    report = DoctorReport()
    _check_python(report)
    _check_cwd(report)

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        report.checks.append(CheckResult("config_load", False, "fatal", str(exc)))
        return report
    except Exception as exc:
        report.checks.append(CheckResult("config_load", False, "fatal", f"parse error: {exc}"))
        return report
    report.checks.append(CheckResult("config_load", True, "info", str(config_path or "default")))

    data_dir = expand_path(config.get("data_dir") or "~/.mini-agent/gateway")
    _check_data_dir(report, data_dir)

    server_cfg = config.get("server") or {}
    _check_port_free(report, server_cfg.get("host", "0.0.0.0"), int(server_cfg.get("port", 8645)))

    log_cfg = config.get("logging") or {}
    log_file = Path(log_cfg["file"]) if log_cfg.get("file") else None
    _check_logging(report, log_file)

    _check_wecom(report, (config.get("platforms") or {}).get("wecom") or {})
    _check_weixin(report, (config.get("platforms") or {}).get("weixin") or {}, data_dir)

    locks_cfg = config.get("locks") or {}
    lock_manager = PlatformLockManager(
        locks_dir=Path(locks_cfg.get("dir") or str(data_dir / "locks")),
        enabled=bool(locks_cfg.get("enabled", True)),
        stale_after_seconds=int(locks_cfg.get("stale_after_seconds", 86400)),
        check_hermes=bool(locks_cfg.get("check_hermes", False)),
        hermes_lock_dir=(
            Path(locks_cfg["hermes_lock_dir"]) if locks_cfg.get("hermes_lock_dir") else None
        ),
    )
    _check_platform_locks(report, config, lock_manager, data_dir=data_dir)

    _check_task_scheduler(report)

    # At least one platform enabled
    platforms = config.get("platforms") or {}
    any_enabled = any((platforms.get(p) or {}).get("enabled") for p in ("wecom", "weixin"))
    if not any_enabled:
        report.checks.append(CheckResult("enabled_platforms", False, "fatal", "no platform enabled"))
    else:
        report.checks.append(CheckResult("enabled_platforms", True, "info", "ok"))

    return report


def format_report_human(report: DoctorReport) -> str:
    lines: list[str] = []
    for c in report.checks:
        icon = "OK" if c.ok else ("!!" if c.severity == "warning" else "XX")
        lines.append(f"[{icon}] {c.severity.upper():7} {c.name}: {c.message}")
    lines.append("")
    lines.append(f"fatal={report.fatal_count} warning={report.warning_count} overall_ok={report.ok}")
    return "\n".join(lines)


def format_report_json(report: DoctorReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
