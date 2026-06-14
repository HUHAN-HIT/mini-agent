"""Mini-agent gateway CLI entrypoint.

Usage::

    python gateway.py init [--output gateway.yaml] [--force] [--env PATH]
    python gateway.py run [--config gateway.yaml]
    python gateway.py doctor [--config gateway.yaml] [--json]
    python gateway.py login weixin [--config gateway.yaml]
    python gateway.py service install [--name NAME] [--config gateway.yaml]
    python gateway.py service uninstall [--name NAME]
    python gateway.py service start [--name NAME]
    python gateway.py service stop [--name NAME]
    python gateway.py service status [--name NAME] [--config gateway.yaml]

The gateway is **opt-in**: ``pip install`` and ``gateway.py run/doctor`` never
register a system service. Only ``service install`` does, and only after a
successful ``doctor`` pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Ensure ``src`` is importable when invoked from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _setup_logging(config: Optional[dict] = None) -> None:
    log_cfg = (config or {}).get("logging") or {}
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    log_file = log_cfg.get("file")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a .env file into a plain dict (dotenv if available, else manual)."""
    try:
        from dotenv import dotenv_values  # type: ignore

        return {k: v for k, v in dotenv_values(str(path)).items() if v is not None}
    except ImportError:
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                values[key] = value.strip().strip('"').strip("'")
        return values


def _collect_env(env_path: Optional[Path]) -> dict[str, str]:
    """Merge process env with a .env file (file wins, so editing .env + re-init works).

    Search order mirrors the agent's own loader: an explicit ``--env`` path, then
    ``~/.mini-agent/.env``, the project ``.env``, and finally ``./.env``.
    """
    if env_path is not None:
        candidates = [Path(env_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".mini-agent" / ".env",
            _PROJECT_ROOT / ".env",
            Path.cwd() / ".env",
        ]

    chosen: Optional[Path] = None
    file_values: dict[str, str] = {}
    for cand in candidates:
        if cand.exists():
            chosen = cand
            file_values = _read_env_file(cand)
            break

    if chosen is not None:
        print(f"读取 .env: {chosen}", file=sys.stderr)
    else:
        print("未找到 .env，仅使用当前进程环境变量。", file=sys.stderr)

    merged = dict(os.environ)
    merged.update(file_values)
    return merged


def _cmd_init(args: argparse.Namespace) -> int:
    from src.gateway.init import build_config_from_env, enabled_platforms, render_yaml

    env = _collect_env(getattr(args, "env", None))
    config = build_config_from_env(env)
    text = render_yaml(config)

    output = Path(getattr(args, "output", None) or "gateway.yaml")
    if output.exists() and not args.force:
        print(
            f"{output} 已存在；加 --force 覆盖，或用 --output 指定其它路径。",
            file=sys.stderr,
        )
        return 1
    output.write_text(text, encoding="utf-8")

    plats = enabled_platforms(config)
    print(f"已生成 {output}")
    if plats:
        print(f"自动启用的平台: {', '.join(plats)}")
    else:
        print("未检测到完整的平台凭据 —— 所有平台均为关闭状态。")
        print("在 .env 填好某平台的必填项后，重新运行 `python gateway.py init --force`。")
    print("下一步: python gateway.py doctor")
    return 0


def _weixin_creds_present(weixin_cfg: dict, creds_path: Path) -> bool:
    """True if weixin has usable credentials, inline or in the scan-login file."""
    if str(weixin_cfg.get("account_id") or "").strip() and str(weixin_cfg.get("bot_token") or "").strip():
        return True
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(str(data.get("account_id") or "").strip() and str(data.get("bot_token") or "").strip())


def _autoconfigure_for_run(args: argparse.Namespace) -> None:
    """Make ``run`` work from a cold start — the hermes "one command" experience.

    - generate ``gateway.yaml`` from ``.env`` if it's missing (no separate init)
    - re-enable weixin if it has saved credentials but got disabled (e.g. by a
      later ``init --force`` whose ``.env`` lacks WEIXIN_* — the scan flow path)
    - if a terminal is attached and weixin is enabled-but-unauthenticated, or no
      platform is usable at all, run the QR scan inline so the user just scans
      and is connected. Non-interactive (service) runs skip the prompt.
    """
    from src.gateway.config import load_config
    from src.gateway.init import build_config_from_env, render_yaml

    config_path = Path(args.config) if args.config else Path("gateway.yaml")

    if not config_path.exists():
        env = _collect_env(None)
        config_path.write_text(render_yaml(build_config_from_env(env)), encoding="utf-8")
        print(f"未发现配置，已自动生成 {config_path}")

    config = load_config(args.config)
    data_dir = Path(config.get("data_dir") or "~/.mini-agent/gateway").expanduser()
    creds_path = data_dir / "weixin_credentials.json"
    weixin = (config.get("platforms") or {}).get("weixin") or {}
    wecom = (config.get("platforms") or {}).get("wecom") or {}

    # --relogin: force a fresh scan even if credentials already exist.
    if getattr(args, "relogin", False) and sys.stdin.isatty():
        print("强制重新登录个人微信，正在拉取二维码……")
        data_dir.mkdir(parents=True, exist_ok=True)
        from src.gateway.platforms.weixin_ilink import run_login_flow

        if run_login_flow(weixin or {"bot_type": "3"}, creds_path=creds_path):
            _enable_platform_in_yaml(args.config, "weixin")
        return

    has_creds = _weixin_creds_present(weixin, creds_path)

    # Saved credentials but weixin disabled → re-enable so `run` actually starts it.
    if has_creds and not weixin.get("enabled"):
        _enable_platform_in_yaml(args.config, "weixin")
        config = load_config(args.config)
        weixin = (config.get("platforms") or {}).get("weixin") or {}

    weixin_ready = bool(weixin.get("enabled") and has_creds)
    any_ready = weixin_ready or bool(wecom.get("enabled"))

    # Need a weixin scan if it's enabled without creds, or nothing is usable yet.
    if has_creds or any_ready:
        return
    if not sys.stdin.isatty():
        # Service / piped run: don't block on a prompt; doctor & connect() surface the gap.
        return

    print("首次使用：需要登录个人微信，正在拉取二维码（扫码确认后自动配置并启动）……")
    data_dir.mkdir(parents=True, exist_ok=True)
    from src.gateway.platforms.weixin_ilink import run_login_flow

    if run_login_flow(weixin or {"bot_type": "3"}, creds_path=creds_path):
        _enable_platform_in_yaml(args.config, "weixin")


def _cmd_run(args: argparse.Namespace) -> int:
    from src.gateway.config import load_config
    from src.gateway.runner import run_gateway

    try:
        _autoconfigure_for_run(args)
    except KeyboardInterrupt:
        print("\nlogin cancelled")
        return 130

    config = load_config(args.config)
    _setup_logging(config)

    try:
        asyncio.run(run_gateway(args.config))
    except KeyboardInterrupt:
        return 130
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from src.gateway.doctor import format_report_human, format_report_json, run_doctor

    report = run_doctor(args.config)
    if args.json:
        print(format_report_json(report))
    else:
        print(format_report_human(report))
    return 0 if report.ok else 1


def _cmd_login(args: argparse.Namespace) -> int:
    if args.platform != "weixin":
        print(f"login not supported for platform {args.platform}", file=sys.stderr)
        return 2
    try:
        from src.gateway.platforms.weixin_ilink import WeixinIlinkAdapter, run_login_flow
    except ImportError as exc:
        print(f"weixin adapter not available: {exc}", file=sys.stderr)
        return 2

    from src.gateway.config import load_config

    config = load_config(args.config)
    weixin_cfg = (config.get("platforms") or {}).get("weixin") or {}
    if not weixin_cfg:
        print("platforms.weixin not configured", file=sys.stderr)
        return 2

    data_dir = Path(config.get("data_dir") or "~/.mini-agent/gateway").expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    creds_path = data_dir / "weixin_credentials.json"
    try:
        ok = run_login_flow(weixin_cfg, creds_path=creds_path)
    except KeyboardInterrupt:
        print("\nlogin cancelled")
        return 130
    if not ok:
        return 1
    # Scan succeeded → enable the platform so `gateway.py run` starts it without
    # the operator hand-editing yaml (the whole point of "scan to configure").
    _enable_platform_in_yaml(args.config, "weixin")
    return 0


def _enable_platform_in_yaml(config_path: str, platform: str) -> None:
    """Flip ``platforms.<platform>.enabled`` to true in an existing gateway.yaml.

    After a scan login the credentials live in their own file, but the runner
    only starts adapters whose platform is enabled. Persisting enabled=true here
    is what makes ``login`` → ``run`` "just work". Best-effort: a missing or
    unreadable file just prints a hint to flip it manually.
    """
    import yaml

    from src.gateway.config import expand_path

    # ``--config`` defaults to None (load_config then falls back to ./gateway.yaml);
    # mirror that resolution so the enable step doesn't choke on a missing flag.
    path = expand_path(config_path) if config_path else Path("gateway.yaml")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"（无法更新 {path} 的 enabled 标志: {exc}；请手动将 platforms.{platform}.enabled 置为 true）")
        return
    block = raw.setdefault("platforms", {}).setdefault(platform, {})
    if block.get("enabled") is True:
        return
    block["enabled"] = True
    try:
        from src.gateway.init import render_yaml

        path.write_text(render_yaml(raw), encoding="utf-8")
    except Exception as exc:
        print(f"（写回 {path} 失败: {exc}；请手动将 platforms.{platform}.enabled 置为 true）")
        return
    print(f"已将 {path} 中 platforms.{platform}.enabled 置为 true，现在可运行 `gateway.py run`。")


def _cmd_service(args: argparse.Namespace) -> int:
    from src.gateway.config import load_config
    from src.gateway.doctor import format_report_human, run_doctor
    from src.gateway.service import (
        make_service_manager,
        spec_from_config,
        status as service_status,
    )

    config = load_config(args.config)
    _setup_logging(config)
    spec = spec_from_config(config, config_path=args.config)
    if args.name:
        spec.name = args.name

    if args.action == "install":
        report = run_doctor(args.config)
        print(format_report_human(report))
        if not report.ok:
            print("\nDoctor fatal errors must be fixed before service install.", file=sys.stderr)
            return 1
        mgr = make_service_manager()
        mgr.install(spec)
        print(f"\nInstalled scheduled task '{spec.name}' (start_on={spec.start_on})")
        print(f"  python: {spec.python}")
        print(f"  cwd   : {spec.cwd}")
        print(f"  args  : {spec.args}")
        return 0

    if args.action == "uninstall":
        mgr = make_service_manager()
        mgr.uninstall(spec.name)
        print(f"Removed scheduled task '{spec.name}'")
        print("(data_dir, credentials, sessions, and logs are kept)")
        return 0

    if args.action == "start":
        mgr = make_service_manager()
        mgr.start(spec.name)
        print(f"Started '{spec.name}'")
        return 0

    if args.action == "stop":
        mgr = make_service_manager()
        mgr.stop(spec.name)
        Path(spec.status_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        from src.gateway.status import mark_stopped

        mark_stopped(Path(spec.status_file).expanduser(), last_error="stopped by CLI")
        print(f"Stopped '{spec.name}'")
        return 0

    if args.action == "status":
        st = service_status(spec.name, Path(spec.status_file).expanduser())
        print(f"Service name      : {spec.name}")
        print(f"Installed         : {st.installed}")
        print(f"Running           : {st.running}")
        print(f"State (last known): {st.state}")
        print(f"PID               : {st.pid}")
        if st.host and st.port:
            print(f"Listen            : {st.host}:{st.port}")
        if st.enabled_platforms:
            print(f"Platforms         : {', '.join(st.enabled_platforms)}")
        if st.config_path:
            print(f"Config            : {st.config_path}")
        if st.log_path:
            print(f"Log               : {st.log_path}")
        if st.last_error:
            print(f"Last error        : {st.last_error}")
        return 0 if st.installed else 1

    print(f"unknown service action: {args.action}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="path to gateway.yaml",
    )

    parser = argparse.ArgumentParser(prog="gateway.py", description="mini-agent IM gateway")
    parser.add_argument("--config", type=Path, default=None, help="path to gateway.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="从 .env 生成 gateway.yaml（自动判断启用哪些平台）")
    init_p.add_argument("--output", type=Path, default=None, help="输出路径（默认 gateway.yaml）")
    init_p.add_argument("--force", action="store_true", help="覆盖已存在的 gateway.yaml")
    init_p.add_argument("--env", type=Path, default=None, help="指定 .env 路径（默认按惯例查找）")

    run_p = sub.add_parser("run", parents=[config_parent], help="run gateway in foreground")
    run_p.add_argument(
        "--relogin",
        action="store_true",
        help="启动前强制重新扫码登录个人微信（覆盖已有凭据）",
    )
    sub.add_parser(
        "doctor",
        parents=[config_parent],
        help="validate config and environment",
    ).add_argument("--json", action="store_true")

    login = sub.add_parser("login", parents=[config_parent], help="interactive credential login")
    login.add_argument("platform", choices=["weixin"])

    svc = sub.add_parser("service", parents=[config_parent], help="manage system autostart")
    svc.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    svc.add_argument("--name", default=None, help="override service name")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load .env into the process environment so settings like HTTP(S)_PROXY are
    # honored by run/doctor/login without juggling shell-specific export syntax
    # (cmd `set` vs PowerShell `$env:`). Existing OS env vars take precedence.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if args.command == "init":
        return _cmd_init(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "login":
        return _cmd_login(args)
    if args.command == "service":
        return _cmd_service(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
