#!/usr/bin/env python3
"""
models.py — Phase 7.2: multi-tenant PostgreSQL schema (SQLAlchemy declarative).

Three tables that replace the old single-user SQLite logs. EVERY table carries a
`username` column — this is the multi-tenant divider. No row exists without an
owner, and every query in the service layer filters by it, so one tenant can
never read another tenant's data.

  * UserSession  — one row per authenticated WebSocket session (login/logout).
  * WindowLog    — the active-window timeline (the Phase 6.1 "eyes" feed).
  * FileTracking — historical record of every saved file's raw text.

`username` is indexed on each table because it is on the hot path of every
per-tenant lookup.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC default for created/updated timestamps."""
    return datetime.now(timezone.utc)


class UserSession(Base):
    """One authenticated client session. Opened on auth, closed on disconnect."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Multi-tenant divider — who this session belongs to.
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Optional client fingerprint (ip / user-agent / device label).
    client_info: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<UserSession user={self.username!r} status={self.status!r}>"


class WindowLog(Base):
    """A single active-window observation streamed from a client's native eyes."""

    __tablename__ = "window_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    app_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    event: Mapped[str] = mapped_column(String(64), nullable=False, default="switch")
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<WindowLog user={self.username!r} app={self.app_name!r}>"


class FileTracking(Base):
    """
    Historical log of a saved file's raw text. This is the relational/audit
    record; the *searchable* copy lives in the user's ChromaDB codebase vault
    (see memory_manager.py). Both are written together by data_service.
    """

    __tablename__ = "file_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    file_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # SHA-1 of the content, so callers can dedupe unchanged saves.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FileTracking user={self.username!r} file={self.file_name!r}>"
