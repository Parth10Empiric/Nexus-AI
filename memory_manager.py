#!/usr/bin/env python3
"""
memory_manager.py — Phase 7.2: dynamic, per-tenant ChromaDB vault router.

The multi-tenant evolution of the Phase 5.2 singleton. Instead of ONE global
pair of collections, every user gets their OWN isolated pair, created on demand:

    {username}_codebase_vault   — that user's source-code chunks
    {username}_activity_vault   — that user's activity timeline

There is no shared collection, so a query for `friend_a` can only ever touch
`friend_a_*` collections — tenancy isolation is structural, not a WHERE clause.

One persistent client is shared process-wide (Chroma is SQLite-backed; multiple
clients on one directory cause "database is locked"). A re-entrant lock
serialises access, and resolved collections are cached so repeated calls for the
same user don't re-hit Chroma's collection registry.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import chromadb

log = logging.getLogger("nexus.memory")

# Persistent on-disk vector store (env-overridable for deployment).
CHROMA_DIR = Path(os.getenv("NEXUS_CHROMA_DIR", "nexus_memory_db"))

# Chroma collection names must be 3-63 chars, alphanumeric/_/-/., start+end
# alphanumeric. We sanitise the username so an odd handle can't break naming.
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_username(username: str) -> str:
    """Sanitise a username into a collection-name-safe token."""
    clean = _SAFE_NAME.sub("_", (username or "").strip())
    if not clean:
        raise ValueError("username is required to resolve a vault")
    # Guarantee an alphanumeric start (Chroma rejects leading _/-/.).
    if not clean[0].isalnum():
        clean = f"u{clean}"
    return clean[:48]  # leave room for the "_codebase_vault" suffix


def _clean_meta(meta: dict) -> dict:
    """Chroma metadata values must be str/int/float/bool — never None."""
    out: dict = {}
    for k, v in meta.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


class TenantMemoryManager:
    """
    Process-wide router that hands each tenant their own Chroma collections.

    Usage:
        mem = TenantMemoryManager()
        codebase, timeline = mem.get_user_vaults("friend_a")
        codebase.upsert(ids=..., embeddings=..., documents=..., metadatas=...)
    """

    _instance: Optional["TenantMemoryManager"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, persist_dir: Optional[Path] = None) -> "TenantMemoryManager":
        with cls._singleton_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instance = inst
            return cls._instance

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        with self._singleton_lock:
            if getattr(self, "_initialized", False):
                return
            self._lock = threading.RLock()
            path = Path(persist_dir or CHROMA_DIR)
            path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(path))
            # Cache of {username: (codebase_collection, timeline_collection)}.
            self._vaults: Dict[str, Tuple] = {}
            self._initialized = True
            log.info("TenantMemoryManager ready at %s", path)

    # ------------------------------------------------------------------ #
    # Dynamic per-user collection routing
    # ------------------------------------------------------------------ #
    def get_user_vaults(self, username: str) -> Tuple:
        """
        Fetch (or lazily create) THIS user's two isolated collections and return
        them as `(codebase_vault, activity_vault)`. Cached after first call.
        """
        key = _safe_username(username)
        with self._lock:
            cached = self._vaults.get(key)
            if cached is not None:
                return cached

            user_codebase = self._client.get_or_create_collection(
                name=f"{key}_codebase_vault",
                metadata={"hnsw:space": "cosine", "tenant": key, "kind": "codebase"},
            )
            user_timeline = self._client.get_or_create_collection(
                name=f"{key}_activity_vault",
                metadata={"hnsw:space": "cosine", "tenant": key, "kind": "activity"},
            )
            self._vaults[key] = (user_codebase, user_timeline)
            log.info("Vaults ready for tenant %r (codebase + activity).", key)
            return self._vaults[key]

    # Convenience accessors -------------------------------------------------- #
    def codebase_vault(self, username: str):
        return self.get_user_vaults(username)[0]

    def activity_vault(self, username: str):
        return self.get_user_vaults(username)[1]

    def upsert_code_chunks(
        self,
        username: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict],
    ) -> None:
        """Isolated upsert into ONLY this user's codebase vault."""
        codebase = self.codebase_vault(username)
        with self._lock:
            codebase.upsert(
                ids=list(ids),
                embeddings=[list(e) for e in embeddings],
                documents=list(documents),
                metadatas=[_clean_meta(m) for m in metadatas],
            )

    def delete_file_chunks(self, username: str, file_path: str) -> None:
        """Drop any prior chunks for `file_path` so a re-save replaces cleanly."""
        codebase = self.codebase_vault(username)
        with self._lock:
            codebase.delete(where={"file_path": file_path})


# Module-level singleton accessor (mirrors the Phase 5 `get_memory()` style).
def get_memory() -> TenantMemoryManager:
    return TenantMemoryManager()
