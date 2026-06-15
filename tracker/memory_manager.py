"""
memory_manager.py — Phase 5.2 "Local Database Memory Management".

A thread-safe SINGLETON that owns the one persistent ChromaDB client for the
whole process and exposes clean CRUD over two strictly separate collections:

  * codebase_index   — source-code chunks (metadata: absolute path, line range)
  * activity_memory  — chronological work logs (metadata: unix ts, app name)

Why a singleton: ChromaDB's PersistentClient is backed by SQLite. Opening two
clients on the same directory from two threads (the watchdog daemon AND the
voice orchestrator) is the classic recipe for "database is locked" errors. One
shared client + one re-entrant lock serializes every access, so concurrent
writes/reads can never collide.

All embeddings are computed elsewhere (Phase 5.1 / nomic-embed-text) and passed
in — this layer is pure storage, so it stays fast and testable.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence

import chromadb

from . import config

log = logging.getLogger("nexus.memory")


def _clean_meta(meta: dict) -> dict:
    """Chroma metadata values must be str/int/float/bool — never None."""
    out = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


class NexusMemoryManager:
    """Process-wide singleton. `NexusMemoryManager()` always returns the same
    instance; the first call fixes the persist directory."""

    _instance: Optional["NexusMemoryManager"] = None
    _singleton_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Singleton construction
    # ------------------------------------------------------------------ #
    def __new__(cls, persist_dir: Optional[Path] = None) -> "NexusMemoryManager":
        with cls._singleton_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instance = inst
            return cls._instance

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        # Guard against re-initialization on repeated NexusMemoryManager() calls.
        with self._singleton_lock:
            if getattr(self, "_initialized", False):
                return

            # The lock that serializes ALL DB access across threads.
            self._lock = threading.RLock()

            path = Path(persist_dir or config.NEXUS_MEMORY_DIR)
            path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(path))

            self._code = self._client.get_or_create_collection(
                config.CODEBASE_COLLECTION, metadata={"hnsw:space": "cosine"})
            self._activity = self._client.get_or_create_collection(
                config.ACTIVITY_COLLECTION, metadata={"hnsw:space": "cosine"})

            self._initialized = True
            log.info("NexusMemoryManager ready at %s", path)

    @classmethod
    def _reset_singleton(cls) -> None:
        """Test-only: drop the singleton so the next construction is fresh."""
        with cls._singleton_lock:
            cls._instance = None

    # ------------------------------------------------------------------ #
    # codebase_index
    # ------------------------------------------------------------------ #
    def upsert_code_chunk(
        self,
        file_path: str,
        chunk_content: str,
        embedding: Sequence[float],
        chunk_index: int = 0,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        content_hash: Optional[str] = None,
    ) -> str:
        """Insert/replace a single code chunk. Returns its stable id."""
        abs_path = str(Path(file_path).resolve())
        doc_id = f"{abs_path}::{chunk_index}"
        meta = _clean_meta({
            "path": abs_path,
            "file_name": Path(abs_path).name,
            "chunk_index": chunk_index,
            "start_line": start_line if start_line is not None else -1,
            "end_line": end_line if end_line is not None else -1,
            "content_hash": content_hash,
        })
        with self._lock:
            self._code.upsert(ids=[doc_id], embeddings=[list(embedding)],
                              documents=[chunk_content], metadatas=[meta])
        return doc_id

    def get_file_hash(self, file_path: str) -> Optional[str]:
        """Return the stored content_hash for a file's chunks, or None if the
        file isn't indexed. Lets callers skip re-embedding unchanged files."""
        abs_path = str(Path(file_path).resolve())
        with self._lock:
            try:
                res = self._code.get(where={"path": abs_path}, limit=1,
                                     include=["metadatas"])
            except Exception:
                return None
        metas = res.get("metadatas") or []
        if metas and isinstance(metas[0], dict):
            return metas[0].get("content_hash")
        return None

    def delete_file(self, file_path: str) -> None:
        """Remove all chunks belonging to one file (use before re-indexing)."""
        abs_path = str(Path(file_path).resolve())
        with self._lock:
            try:
                self._code.delete(where={"path": abs_path})
            except Exception as exc:
                log.debug("delete_file %s: %s", abs_path, exc)

    def query_codebase(self, query_embedding: Sequence[float],
                       n_results: int = 3) -> List[dict]:
        return self._query(self._code, query_embedding, n_results)

    # ------------------------------------------------------------------ #
    # activity_memory
    # ------------------------------------------------------------------ #
    def upsert_activity_log(
        self,
        timestamp,
        log_content: str,
        embedding: Sequence[float],
        app_name: str = "",
        title: str = "",
        log_id: Optional[str] = None,
    ) -> str:
        """Insert/replace one activity-log memory. `timestamp` may be a Unix
        int/float or an ISO string; it's normalized to a Unix int in metadata."""
        unix_ts = self._to_unix(timestamp)
        doc_id = log_id or f"log-{unix_ts}-{abs(hash(log_content)) % 10_000}"
        meta = _clean_meta({
            "unix_ts": unix_ts,
            "app_name": app_name,
            "title": title,
        })
        with self._lock:
            self._activity.upsert(ids=[doc_id], embeddings=[list(embedding)],
                                  documents=[log_content], metadatas=[meta])
        return doc_id

    def query_activity(self, query_embedding: Sequence[float],
                       n_results: int = 5) -> List[dict]:
        return self._query(self._activity, query_embedding, n_results)

    # ------------------------------------------------------------------ #
    # shared
    # ------------------------------------------------------------------ #
    def _query(self, collection, query_embedding: Sequence[float],
               n_results: int) -> List[dict]:
        with self._lock:
            res = collection.query(
                query_embeddings=[list(query_embedding)],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        out: List[dict] = []
        if not res.get("ids") or not res["ids"][0]:
            return out
        for i, doc_id in enumerate(res["ids"][0]):
            out.append({
                "id": doc_id,
                "document": res["documents"][0][i],
                "metadata": res["metadatas"][0][i],
                "distance": res["distances"][0][i],
            })
        return out

    @staticmethod
    def _to_unix(timestamp) -> int:
        if isinstance(timestamp, (int, float)):
            return int(timestamp)
        # Try to parse an ISO-8601 string.
        try:
            from datetime import datetime
            ts = str(timestamp).replace("Z", "+00:00")
            return int(datetime.fromisoformat(ts).timestamp())
        except Exception:
            return int(time.time())

    def stats(self) -> dict:
        with self._lock:
            return {
                "codebase_chunks": self._code.count(),
                "activity_entries": self._activity.count(),
            }


def get_memory() -> NexusMemoryManager:
    """Convenience accessor for the shared singleton."""
    return NexusMemoryManager()
