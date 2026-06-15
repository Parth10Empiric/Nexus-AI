"""
SQLite storage layer for the tracker.

We use SQLite (built into Python's stdlib, zero external dependency) rather
than a JSON file because:
  * it appends safely without rewriting the whole file each time,
  * it's queryable (Phase 2's RAG / Phase 4's reports will SELECT from it),
  * it stays tiny on disk.

Each row is the START of a focus session: "at time T the user switched to
window X". A clean timeline falls out of reading the rows in order.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .window_source import WindowSample

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT    NOT NULL,   -- ISO-8601 UTC timestamp
    app_name    TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    pid         INTEGER,
    event       TEXT    NOT NULL DEFAULT 'switch'  -- 'switch' or 'heartbeat'
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log (ts_utc);

-- Phase 3.1: the source text of the file the user is actively editing.
-- One row per absolute_path (UNIQUE) so we OVERWRITE in place: the table
-- always holds the latest snapshot of each file we've seen, not a history.
CREATE TABLE IF NOT EXISTS active_file_context (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT    NOT NULL,
    window_title  TEXT    NOT NULL,
    app_name      TEXT    NOT NULL,
    file_name     TEXT    NOT NULL,
    absolute_path TEXT    NOT NULL UNIQUE,
    file_content  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_ctx_ts ON active_file_context (ts_utc);
"""


class ActivityStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the main window-tracker thread AND the
        # Phase 3.2 watchdog observer thread share this connection. Every write
        # is serialized by self._lock, so concurrent access is safe.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def log(self, sample: WindowSample, event: str = "switch") -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity_log (ts_utc, app_name, title, pid, event) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, sample.app_name, sample.title, sample.pid, event),
            )
            self._conn.commit()

    def save_file_context(
        self,
        *,
        window_title: str,
        app_name: str,
        file_name: str,
        absolute_path: str,
        file_content: str,
    ) -> None:
        """
        Insert or OVERWRITE the active-file snapshot for `absolute_path`.

        Uses SQLite's UPSERT: on the first sighting it INSERTs; on every later
        sighting of the same path it UPDATEs the timestamp and content in
        place, so the table never grows unbounded with stale copies.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO active_file_context
                    (ts_utc, window_title, app_name, file_name, absolute_path, file_content)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(absolute_path) DO UPDATE SET
                    ts_utc       = excluded.ts_utc,
                    window_title = excluded.window_title,
                    app_name     = excluded.app_name,
                    file_name    = excluded.file_name,
                    file_content = excluded.file_content
                """,
                (ts, window_title, app_name, file_name, absolute_path, file_content),
            )
            self._conn.commit()

    def latest_file_context(self) -> Optional[sqlite3.Row]:
        """Return the most recently updated file-context row, or None."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(
                "SELECT ts_utc, app_name, file_name, absolute_path, "
                "LENGTH(file_content) AS content_len "
                "FROM active_file_context ORDER BY ts_utc DESC LIMIT 1"
            )
            return cur.fetchone()

    def latest_file_context_full(self) -> Optional[sqlite3.Row]:
        """
        Return the full most-recent file-context row INCLUDING file_content.
        This is the row the Phase 3.3 prompt mixer consumes. Indexed single-row
        read -> sub-millisecond.
        """
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(
                "SELECT ts_utc, window_title, app_name, file_name, "
                "absolute_path, file_content "
                "FROM active_file_context ORDER BY ts_utc DESC LIMIT 1"
            )
            return cur.fetchone()

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(
                "SELECT ts_utc, app_name, title, event FROM activity_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
