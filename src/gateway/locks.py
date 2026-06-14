"""Platform resource single-owner locks.

Why this exists (see docs/im-gateway-design.md §10): a single WeCom
``corp_id:agent_id`` callback or a single iLink ``bot_token`` cannot be polled
by two processes simultaneously — they would advance each other's sync cursor
and corrupt context_token caches. This module hands mini-agent and hermes a
shared lock identity so the second process knows not to start.

Design:

- One lock file per ``(scope, identity)`` pair under ``locks_dir``.
- ``identity`` for token-based scopes is the short SHA-256 of the token —
  never the raw token. For ``corp_id:agent_id`` we use the unhashed value
  because corp_id/agent_id are not secrets.
- Lock file holds diagnostic metadata only (owner, pid, cwd, started_at).
  No credentials are written.
- Stale locks (pid dead) are detected and can be cleaned with explicit
  ``force_stale_lock``. Live locks are never auto-stolen.

This is cooperative — a malicious process can ignore it. The goal is to make
honest mistakes loud, not to enforce isolation against active abuse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.gateway.router import atomic_write_json, load_json

logger = logging.getLogger(__name__)


@dataclass
class LockInfo:
    owner: str = "mini-agent"
    platform: str = ""
    scope: str = ""
    identity: str = ""
    pid: int = 0
    cwd: str = ""
    command: str = ""
    config_path: str = ""
    started_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["host"] = platform.node()
        return data


@dataclass
class AcquireResult:
    acquired: bool
    owner_info: Optional[LockInfo]
    stale: bool = False
    error: Optional[str] = None
    lock_path: Optional[str] = None


def _identity_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except ProcessLookupError:
        return False


class PlatformLockManager:
    """Acquire/release platform resource locks under ``locks_dir``.

    One manager per gateway process. ``locks.enabled=False`` skips writes but
    still returns ``AcquireResult(acquired=True)`` so test setups don't have to
    branch on lock state.
    """

    def __init__(
        self,
        *,
        locks_dir: Path,
        enabled: bool = True,
        stale_after_seconds: int = 86400,
        owner: str = "mini-agent",
        hermes_lock_dir: Optional[Path] = None,
        check_hermes: bool = False,
    ) -> None:
        self._dir = locks_dir
        self._enabled = enabled
        self._stale_after = stale_after_seconds
        self._owner = owner
        self._hermes_dir = hermes_lock_dir
        self._check_hermes = check_hermes
        self._acquired: set[str] = set()

        if enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # identity helpers
    # ------------------------------------------------------------------
    @staticmethod
    def identity_for_token(token: str) -> str:
        return _identity_hash(token) if token else ""

    @staticmethod
    def identity_for_app(corp_id: str, agent_id: str) -> str:
        return f"{corp_id}:{agent_id}"

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------
    def acquire(
        self,
        *,
        scope: str,
        identity: str,
        platform: str,
        config_path: str = "",
        command: str = "",
        extra: Optional[dict] = None,
        force_stale_lock: bool = False,
    ) -> AcquireResult:
        if not identity:
            return AcquireResult(acquired=False, owner_info=None, error="empty identity")
        if not self._enabled:
            self._acquired.add(f"{scope}:{identity}")
            return AcquireResult(acquired=True, owner_info=None)

        path = self._lock_path(scope, identity)
        existing = self._read(path)
        if existing is not None:
            stale = self._is_stale(existing)
            if stale:
                if not force_stale_lock:
                    return AcquireResult(
                        acquired=False,
                        owner_info=existing,
                        stale=True,
                        error="stale lock; pass force_stale_lock=True to clean",
                        lock_path=str(path),
                    )
                logger.info("removing stale lock %s (pid=%s)", path, existing.pid)
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                return AcquireResult(
                    acquired=False,
                    owner_info=existing,
                    stale=False,
                    error=f"{scope}/{identity} held by pid {existing.pid}",
                    lock_path=str(path),
                )

        # Hermes compatibility: detect same identity in hermes lock dir.
        if self._check_hermes and self._hermes_dir is not None:
            hermes_info = self._detect_hermes(scope, identity)
            if hermes_info is not None:
                return AcquireResult(
                    acquired=False,
                    owner_info=hermes_info,
                    stale=False,
                    error=f"{scope}/{identity} held by hermes",
                    lock_path=str(self._hermes_dir),
                )

        info = LockInfo(
            owner=self._owner,
            platform=platform,
            scope=scope,
            identity=identity,
            pid=os.getpid(),
            cwd=str(Path.cwd()),
            command=command or " ".join(sys.argv[:6]),
            config_path=config_path,
            started_at=datetime.now().isoformat(),
            extra=dict(extra or {}),
        )
        try:
            atomic_write_json(path, info.to_dict())
        except OSError as exc:
            return AcquireResult(acquired=False, owner_info=None, error=str(exc))
        self._acquired.add(f"{scope}:{identity}")
        return AcquireResult(acquired=True, owner_info=None, lock_path=str(path))

    def release(self, *, scope: str, identity: str) -> bool:
        key = f"{scope}:{identity}"
        if key not in self._acquired:
            return False
        self._acquired.discard(key)
        if not self._enabled:
            return True
        path = self._lock_path(scope, identity)
        try:
            if path.exists():
                path.unlink()
            return True
        except OSError:
            return False

    def release_all(self) -> None:
        for key in list(self._acquired):
            scope, _, identity = key.partition(":")
            self.release(scope=scope, identity=identity)

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def describe(self, *, scope: str, identity: str) -> Optional[LockInfo]:
        if not identity:
            return None
        path = self._lock_path(scope, identity)
        info = self._read(path)
        if info is None and self._check_hermes and self._hermes_dir is not None:
            info = self._detect_hermes(scope, identity)
        return info

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _lock_path(self, scope: str, identity: str) -> Path:
        safe_identity = identity.replace(":", "-").replace("/", "-").replace("\\", "-")
        return self._dir / scope / f"{safe_identity}.json"

    def _read(self, path: Path) -> Optional[LockInfo]:
        if not path.exists():
            return None
        data = load_json(path, default=None)
        if not isinstance(data, dict):
            return None
        try:
            return LockInfo(
                owner=str(data.get("owner", "unknown")),
                platform=str(data.get("platform", "")),
                scope=str(data.get("scope", "")),
                identity=str(data.get("identity", "")),
                pid=int(data.get("pid", 0) or 0),
                cwd=str(data.get("cwd", "")),
                command=str(data.get("command", "")),
                config_path=str(data.get("config_path", "")),
                started_at=str(data.get("started_at", "")),
                extra=dict(data.get("extra") or {}),
            )
        except Exception:
            return None

    def _is_stale(self, info: LockInfo) -> bool:
        if not _pid_alive(info.pid):
            return True
        try:
            started = datetime.fromisoformat(info.started_at).timestamp()
        except (ValueError, OSError):
            return False
        if time.time() - started > self._stale_after:
            return True
        return False

    def _detect_hermes(self, scope: str, identity: str) -> Optional[LockInfo]:
        """Best-effort detection of the same resource held by hermes.

        P0 doesn't parse hermes' internal state; we just look for a lock file
        with matching scope+identity in ``hermes_lock_dir`` if the user
        configured it. If hermes doesn't write lock files at that path, we
        return None and doctor will warn the user to set the path correctly.
        """

        if not identity or self._hermes_dir is None:
            return None
        safe = identity.replace(":", "-").replace("/", "-").replace("\\", "-")
        candidate = self._hermes_dir / scope / f"{safe}.json"
        info = self._read(candidate)
        if info is None:
            candidate2 = self._hermes_dir / f"{scope}-{safe}.json"
            info = self._read(candidate2)
        return info


def lock_identity_for_adapter(
    *,
    platform: str,
    bot_token: str = "",
    corp_id: str = "",
    agent_id: str = "",
) -> tuple[str, str]:
    """Resolve ``(scope, identity)`` for a platform config block."""

    if platform == "weixin":
        return "weixin-bot-token", PlatformLockManager.identity_for_token(bot_token)
    if platform == "wecom":
        return "wecom-app", PlatformLockManager.identity_for_app(corp_id, agent_id)
    if platform == "telegram":
        return "telegram-bot-token", PlatformLockManager.identity_for_token(bot_token)
    return platform, ""
