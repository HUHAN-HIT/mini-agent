"""SQLite FTS5 session search index for cross-session full-text search."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path.home() / ".mini-agent" / "sessions.db"


@dataclass(frozen=True)
class SearchMatch:
    session_id: str
    title: str
    started_at: str
    message_count: int
    snippet: str
    rank: float

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "title": self.title, "started_at": self.started_at, "message_count": self.message_count, "snippet": self.snippet}


class SessionSearchIndex:
    """SQLite FTS5 index for cross-session search."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, message_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        """)
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content=messages, content_rowid=id)")
        except sqlite3.OperationalError:
            pass
        for trigger_sql in [
            "CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END",
            "CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content); END",
        ]:
            try:
                conn.execute(trigger_sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()

    def index_session(self, session_id: str, title: str = "") -> None:
        conn = self._get_conn()
        conn.execute("INSERT OR REPLACE INTO sessions (id, title, started_at, message_count) VALUES (?, ?, ?, COALESCE((SELECT message_count FROM sessions WHERE id = ?), 0))", (session_id, title, time.time(), session_id))
        conn.commit()

    def index_message(self, session_id: str, role: str, content: str) -> None:
        if not content or not content.strip():
            return
        conn = self._get_conn()
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)", (session_id, role, content[:50_000], time.time()))
        conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (session_id,))
        conn.commit()

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        import re as _re
        tokens = _re.findall(r"[a-zA-Z0-9_]{2,}|[一-鿿㐀-䶿]", query)
        if not tokens:
            return '""'
        return " OR ".join(f'"{t}"' for t in tokens)

    def search(self, query: str, max_sessions: int = 3) -> List[SearchMatch]:
        conn = self._get_conn()
        fts_query = self._sanitize_fts_query(query)
        try:
            cursor = conn.execute("""
                SELECT m.session_id, s.title, s.started_at, s.message_count,
                       snippet(messages_fts, 0, '>>>', '<<<', '...', 64) AS snippet, rank
                FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                JOIN sessions s ON s.id = m.session_id WHERE messages_fts MATCH ?
                ORDER BY rank LIMIT ?
            """, (fts_query, max_sessions * 5))
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 search failed: %s", exc)
            return []
        seen: dict[str, SearchMatch] = {}
        for row in cursor.fetchall():
            sid = row[0]
            if sid in seen:
                continue
            seen[sid] = SearchMatch(session_id=row[0], title=row[1] or "(untitled)", started_at=self._format_time(row[2]), message_count=row[3], snippet=row[4], rank=row[5])
            if len(seen) >= max_sessions:
                break
        return list(seen.values())

    @staticmethod
    def _format_time(epoch: float) -> str:
        try:
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            return "unknown"

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


import threading as _threading

_shared_index: Optional[SessionSearchIndex] = None
_shared_lock = _threading.Lock()


def get_shared_index() -> SessionSearchIndex:
    global _shared_index
    if _shared_index is None:
        with _shared_lock:
            if _shared_index is None:
                _shared_index = SessionSearchIndex()
    return _shared_index
