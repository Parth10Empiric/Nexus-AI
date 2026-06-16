#!/usr/bin/env python3
"""
database.py — Phase 7.2: PostgreSQL enterprise core (SQLAlchemy engine).

Replaces the old per-machine SQLite3 storage with a centralized, pooled
PostgreSQL engine that the multi-tenant SaaS server shares across every request.

Two things this file gets right that bite people:

  1. PASSWORD URL-ENCODING. The DB password `Postgres@1011` contains an `@`,
     which is the userinfo/host delimiter in a URL. Left raw, SQLAlchemy would
     parse `...:Postgres@1011@host...` as host=`1011@host` and blow up. We run
     the password through `urllib.parse.quote_plus` → `Postgres%401011`, so the
     connection string is always well-formed regardless of special characters.

  2. POOL SIZING. `pool_size=20, max_overflow=0` gives a fixed, predictable pool
     of 20 connections and refuses to silently open more under load — so we never
     exhaust Postgres' connection limit from a runaway client.

Everything else (the tenant-divided tables) lives in `models.py`, which imports
`Base` from here.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

log = logging.getLogger("nexus.database")

# ─────────────────────────────────────────────────────────────────────────────
# Connection parameters (env-overridable; defaults match the Phase 7.2 spec).
# ─────────────────────────────────────────────────────────────────────────────
DB_USER = os.getenv("NEXUS_PG_USER", "postgres")
DB_PASSWORD = os.getenv("NEXUS_PG_PASSWORD", "Postgres@1011")
DB_HOST = os.getenv("NEXUS_PG_HOST", "localhost")
DB_PORT = os.getenv("NEXUS_PG_PORT", "5432")
DB_NAME = os.getenv("NEXUS_PG_DB", "postgres")

# CRITICAL FIX: URL-encode the password so the `@` (and any future special
# chars) can never corrupt the connection-string parse.
#   "Postgres@1011" -> "Postgres%401011"
_ENCODED_PASSWORD = urllib.parse.quote_plus(DB_PASSWORD)

DATABASE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{_ENCODED_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ─────────────────────────────────────────────────────────────────────────────
# Engine + session factory + declarative base (the three things callers need).
# ─────────────────────────────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_size=20,        # fixed pool of 20 live connections
    max_overflow=0,      # never open beyond the pool — fail fast under overload
    pool_pre_ping=True,  # transparently recycle connections dropped by Postgres
    future=True,
)

# autocommit/autoflush off → explicit, predictable transaction boundaries.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# The declarative base every model in `models.py` inherits from.
Base = declarative_base()


@contextmanager
def get_session() -> Iterator[Session]:
    """
    Transactional scope around a series of operations. Commits on success,
    rolls back on any exception, and always closes the session back to the pool.

        with get_session() as db:
            db.add(row)
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """
    Create every table defined in `models.py` if it does not already exist.
    Importing `models` here (lazily) ensures the classes are registered on
    `Base.metadata` before `create_all` runs. Call once at server startup.
    """
    import models  # noqa: F401 — side-effect import registers the tables

    Base.metadata.create_all(bind=engine)
    log.info("PostgreSQL schema ready (host=%s db=%s).", DB_HOST, DB_NAME)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Handy one-shot: `python database.py` creates the schema and reports.
    init_db()
    print(f"Connected & schema ensured: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
