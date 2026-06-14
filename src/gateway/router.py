"""Map stable gateway session keys to mini-agent session ids.

The gateway session key (see ``session_key.build_session_key``) is the
platform-facing identity; the mini-agent session_id is the internal agent
history. They are 1:1 per gateway install and persisted across restarts so a
user restarting the gateway keeps their conversation.

Storage is JSON-on-disk with atomic write (temp + replace) so a crash mid-write
doesn't corrupt the whole map.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from src.gateway.base import SessionSource
from src.gateway.session_key import build_session_key

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically by writing to a temp file and renaming."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default() if callable(default) else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("router map at %s is unreadable, falling back to default", path)
        return default() if callable(default) else default


class SessionRouter:
    """Map stable gateway session keys to mini-agent session ids.

    The router holds an in-memory cache and writes through to disk on every
    new mapping. The map is small (one entry per active chat) so the disk
    write cost is negligible.
    """

    def __init__(self, service: Any, path: Path, config: dict) -> None:
        self._service = service
        self._path = path
        self._group_sessions_per_user = bool(config.get("group_sessions_per_user", True))
        self._thread_sessions_per_user = bool(config.get("thread_sessions_per_user", False))
        self._map: dict[str, str] = load_json(path, default=dict) or {}

    def session_key(self, source: SessionSource) -> str:
        return build_session_key(
            source,
            group_sessions_per_user=self._group_sessions_per_user,
            thread_sessions_per_user=self._thread_sessions_per_user,
        )

    def get_or_create(self, source: SessionSource) -> tuple[str, str]:
        session_key = self.session_key(source)
        session_id = self._map.get(session_key)
        if session_id is None:
            session = self._service.create_session(title=session_key)
            session_id = session.session_id
            self._map[session_key] = session_id
            try:
                atomic_write_json(self._path, self._map)
            except OSError as exc:
                logger.warning("router map write failed (%s); in-memory entry still valid", exc)
        return session_id, session_key

    def known(self, session_key: str) -> str | None:
        return self._map.get(session_key)

    def snapshot(self) -> dict[str, str]:
        return dict(self._map)
