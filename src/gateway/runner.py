"""Gateway runner: wire adapters -> router -> agent turn -> delivery.

Single async entry point ``run_gateway(config_path)``:

1. Load config, build SessionStore/EventBus/SessionService (reuse mini-agent's
   existing stack — the gateway is a thin control plane).
2. Build adapters, acquire their resource locks, register the message handler.
3. Start a web server if any webhook-based platform is enabled, and let
   long-poll adapters run their own background tasks.
4. Wait for shutdown; release locks and disconnect adapters.

The handler is the same for every platform: turn inbound MessageEvent into a
serialized mini-agent attempt and deliver the final answer.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Optional

from src.gateway.adapters import AdapterBuild, build_adapters
from src.gateway.base import BasePlatformAdapter, MessageEvent
from src.gateway.config import expand_path, load_config
from src.gateway.delivery import deliver_final_response
from src.gateway.locks import PlatformLockManager
from src.gateway.router import SessionRouter
from src.gateway.status import make_running_status, mark_stopped, write_status
from src.gateway.turn_queue import SessionTurnQueue
from src.session.events import EventBus
from src.session.service import SessionService
from src.session.store import SessionStore

logger = logging.getLogger(__name__)


class GatewayRunner:
    """Owns adapter lifecycle, routing, turn serialization, and shutdown."""

    def __init__(self, config: dict, config_path: Optional[Path] = None) -> None:
        self.config = config
        self.config_path = config_path
        self._shutdown = asyncio.Event()

        data_dir = Path(config.get("data_dir") or "~/.mini-agent/gateway").expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir

        runs_dir = data_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        self.store = SessionStore(data_dir / "sessions")
        self.event_bus = EventBus()
        self.service = SessionService(self.store, self.event_bus, runs_dir)

        session_cfg = config.get("session") or {}
        router_path = Path(session_cfg.get("router_path") or str(data_dir / "sessions_map.json"))
        self.router = SessionRouter(self.service, router_path, session_cfg)
        self.turn_queue = SessionTurnQueue()

        locks_cfg = config.get("locks") or {}
        self.lock_manager = PlatformLockManager(
            locks_dir=Path(locks_cfg.get("dir") or str(data_dir / "locks")),
            enabled=bool(locks_cfg.get("enabled", True)),
            stale_after_seconds=int(locks_cfg.get("stale_after_seconds", 86400)),
            check_hermes=bool(locks_cfg.get("check_hermes", False)),
            hermes_lock_dir=(
                Path(locks_cfg["hermes_lock_dir"]) if locks_cfg.get("hermes_lock_dir") else None
            ),
        )

        self._adapters: list[AdapterBuild] = []
        self._server_task: Optional[asyncio.Task] = None
        self._server: Any = None
        self._app: Any = None

    # ------------------------------------------------------------------
    # message handling
    # ------------------------------------------------------------------
    async def _wait_for_attempt(self, session_id: str, attempt_id: str) -> tuple[str, dict]:
        def from_store() -> tuple[str, dict] | None:
            for attempt in self.service.get_attempts(session_id):
                if attempt.attempt_id != attempt_id:
                    continue
                status = attempt.status.value
                if status not in {"completed", "failed", "cancelled"}:
                    return None
                event_type = "attempt.completed" if status == "completed" else "attempt.failed"
                return event_type, {
                    "attempt_id": attempt_id,
                    "status": status,
                    "content": attempt.summary or "",
                    "error": attempt.error or "",
                    "run_dir": attempt.run_dir,
                }
            return None

        if stored := from_store():
            return stored

        async for event in self.event_bus.subscribe(session_id):
            if event.event_type == "heartbeat":
                if stored := from_store():
                    return stored
                continue
            if event.event_type not in {"attempt.completed", "attempt.failed"}:
                continue
            if event.data.get("attempt_id") != attempt_id:
                continue
            return event.event_type, event.data

        # Should not reach here; treat as failure to avoid hanging.
        return "attempt.failed", {"attempt_id": attempt_id, "error": "event stream closed"}

    async def _handle_event(self, event: MessageEvent) -> None:
        adapter = self._adapter_for_platform(event.source.platform)
        if adapter is None:
            logger.warning("no adapter for platform %s, drop event", event.source.platform)
            return

        session_id, _ = self.router.get_or_create(event.source)

        async def run_turn() -> None:
            try:
                result = await self.service.send_message(session_id, event.text)
            except Exception as exc:
                logger.exception("send_message failed: %s", exc)
                await self._safe_reply(adapter, event, f"(执行失败: {exc})")
                return
            attempt_id = result.get("attempt_id")
            if not attempt_id:
                return  # role != user or duplicate

            event_type, data = await self._wait_for_attempt(session_id, attempt_id)
            if event_type == "attempt.completed":
                content = data.get("content") or "(无回复)"
            else:
                content = f"执行失败：{data.get('error') or 'unknown'}"

            await deliver_final_response(
                adapter=adapter,
                source=event.source,
                content=content,
                reply_to=event.message_id or event.source.message_id,
            )

        await self.turn_queue.run(session_id, run_turn)

    async def _safe_reply(self, adapter: BasePlatformAdapter, event: MessageEvent, text: str) -> None:
        try:
            await deliver_final_response(
                adapter=adapter,
                source=event.source,
                content=text,
                reply_to=event.message_id,
            )
        except Exception:
            logger.exception("safe reply failed")

    def _adapter_for_platform(self, platform: str) -> Optional[BasePlatformAdapter]:
        for build in self._adapters:
            if build.platform == platform:
                return build.adapter
        return None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        """Build adapters, register handler, start webhook server if needed.

        Returns a small status dict (used by doctor and CLI status).
        """

        loop = asyncio.get_running_loop()
        self.event_bus.set_loop(loop)

        # FastAPI app is needed whenever a webhook platform is enabled.
        # Adapters receive the same app and may register their own routes.
        needs_server = self._needs_web_server()
        if needs_server:
            try:
                from fastapi import FastAPI

                self._app = FastAPI(title="mini-agent-gateway", docs_url=None, redoc_url=None)
            except ImportError as exc:
                raise RuntimeError("fastapi is required when webhook platforms are enabled") from exc
        else:
            self._app = None

        self._adapters = build_adapters(
            config=self.config,
            app=self._app,
            lock_manager=self.lock_manager,
            config_path=str(self.config_path) if self.config_path else "",
            command="gateway.py run",
        )

        for build in self._adapters:
            build.adapter.set_message_handler(self._handle_event)

        started: list[dict] = []
        for build in self._adapters:
            if build.adapter.has_fatal_error:
                started.append({
                    "platform": build.platform,
                    "ok": False,
                    "error": build.adapter.fatal_error_message,
                })
                continue
            try:
                ok = await build.adapter.connect()
            except Exception as exc:
                logger.exception("adapter %s connect error", build.platform)
                started.append({"platform": build.platform, "ok": False, "error": str(exc)})
                continue
            started.append({
                "platform": build.platform,
                "ok": ok,
                "error": build.adapter.fatal_error_message if not ok else None,
            })

        if needs_server:
            self._server_task = asyncio.create_task(self._serve_web(), name="gateway-web")

        self._write_running_status(started)

        # Install signal handlers; ignore in environments without signals.
        self._install_signal_handlers()
        return {
            "started": started,
            "needs_server": needs_server,
            "data_dir": str(self.data_dir),
        }

    def _needs_web_server(self) -> bool:
        platforms = self.config.get("platforms") or {}
        return bool((platforms.get("wecom") or {}).get("enabled"))

    async def _serve_web(self) -> None:
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("uvicorn is required when webhook platforms are enabled") from exc

        server_cfg = self.config.get("server") or {}
        host = server_cfg.get("host", "0.0.0.0")
        port = int(server_cfg.get("port", 8645))

        config = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            pass

    def _install_signal_handlers(self) -> None:
        for name in {"SIGINT", "SIGTERM"}:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError):
                # Windows doesn't support add_signal_handler; fall back gracefully.
                pass

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def _status_file(self) -> Optional[Path]:
        service_cfg = self.config.get("service") or {}
        raw = service_cfg.get("status_file")
        return Path(raw).expanduser() if raw else None

    def _write_running_status(self, started: list[dict]) -> None:
        path = self._status_file()
        if path is None:
            return
        server_cfg = self.config.get("server") or {}
        service_cfg = self.config.get("service") or {}
        log_cfg = self.config.get("logging") or {}
        enabled_platforms = [entry["platform"] for entry in started if entry.get("ok")]
        status = make_running_status(
            service_name=service_cfg.get("name") or "mini-agent-gateway",
            config_path=str(self.config_path or ""),
            cwd=str(Path.cwd()),
            python=sys.executable,
            host=server_cfg.get("host", "0.0.0.0"),
            port=int(server_cfg.get("port", 8645)),
            enabled_platforms=enabled_platforms,
            log_path=str(log_cfg.get("file") or ""),
        )
        try:
            write_status(path, status)
        except OSError:
            logger.exception("runtime status write failed")

    async def serve(self) -> None:
        await self._shutdown.wait()

    async def stop(self) -> None:
        logger.info("gateway stopping")
        for build in self._adapters:
            try:
                await build.adapter.disconnect()
            except Exception:
                logger.exception("adapter %s disconnect error", build.platform)
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        self.lock_manager.release_all()
        path = self._status_file()
        if path is not None:
            try:
                mark_stopped(path)
            except OSError:
                logger.exception("runtime status stop marker failed")
        try:
            self.event_bus.clear("")
        except Exception:
            pass


async def run_gateway(config_path: Optional[Path] = None) -> None:
    """Load config, run until shutdown, then clean up."""

    config = load_config(config_path)
    runner = GatewayRunner(config, config_path=config_path)
    status = await runner.start()
    for entry in status["started"]:
        level = logging.INFO if entry["ok"] else logging.WARNING
        logger.log(level, "adapter %s: ok=%s error=%s", entry["platform"], entry["ok"], entry["error"])

    if not any(entry["ok"] for entry in status["started"]):
        logger.error("no adapter started successfully; exiting")
        await runner.stop()
        return

    try:
        await runner.serve()
    finally:
        await runner.stop()
