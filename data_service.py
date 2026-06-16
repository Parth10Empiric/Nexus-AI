#!/usr/bin/env python3
"""
data_service.py — Phase 7.2: the dual-database data router.

The bridge between the FastAPI WebSocket gateway (Phase 7.1) and the two storage
backends. A client sends a file payload; this service fans it out to BOTH:

  1. PostgreSQL  (`file_tracking`)        — durable, relational audit log.
  2. ChromaDB    (`{username}_codebase_vault`) — chunked + embedded for search.

Both writes are scoped to the caller's `username`, so the file lands only in
that tenant's relational rows and only in that tenant's vector vault. Embeddings
reuse the existing local Ollama `nomic-embed-text` model and the same
character-based chunking as the Phase 5 indexer, for index compatibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter

from database import get_session
from memory_manager import get_memory
from models import FileTracking

log = logging.getLogger("nexus.data_service")

# ─────────────────────────────────────────────────────────────────────────────
# Embedding + chunking config (matches tracker/config.py so indexes stay aligned)
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("NEXUS_OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("NEXUS_EMBED_MODEL", "nomic-embed-text")
CHUNK_CHARS = int(os.getenv("NEXUS_CHUNK_CHARS", "2000"))
CHUNK_OVERLAP_CHARS = int(os.getenv("NEXUS_CHUNK_OVERLAP", "200"))

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP_CHARS
)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _embed(texts: List[str]) -> List[List[float]]:
    """
    Embed a batch of strings with the local Ollama `nomic-embed-text` model.
    Returns one vector per input; raises if Ollama is unreachable or the count
    is wrong (so callers can decide whether to abort the upsert).
    """
    if not texts:
        return []
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise RuntimeError("embedding count mismatch from Ollama")
    return embeddings


def process_incoming_file_sync(
    username: str,
    file_path: str,
    file_content: str,
) -> dict:
    """
    Route ONE saved file to both databases, isolated to `username`.

    Step 1 — PostgreSQL: persist the raw text to `file_tracking` (audit/history).
    Step 2 — ChromaDB:   chunk → embed → upsert into `{username}_codebase_vault`,
             replacing any prior chunks for this path so a re-save is clean.

    Returns a summary dict: {"saved": bool, "chunks": int, "skipped": bool}.

    Designed to be called from a thread/executor off the async WebSocket loop
    (hence the `_sync` name): the embed call and DB writes are blocking.
    """
    if not username:
        raise ValueError("username is required")

    content = file_content or ""
    file_name = Path(file_path).name
    content_hash = _hash(content)

    # ── Step 1: PostgreSQL audit log ─────────────────────────────────────────
    # Skip the whole pipeline if this exact content was already logged for this
    # user+path (cheap dedupe — avoids redundant embedding on no-op saves).
    with get_session() as db:
        already = (
            db.query(FileTracking.id)
            .filter(
                FileTracking.username == username,
                FileTracking.file_path == file_path,
                FileTracking.content_hash == content_hash,
            )
            .first()
        )
        if already is not None:
            log.info("skip %s for %r — unchanged (hash match).", file_name, username)
            return {"saved": False, "chunks": 0, "skipped": True}

    # ── Step 2: chunk + embed for the vector vault ───────────────────────────
    chunks = _splitter.split_text(content) if content.strip() else []
    chunk_count = 0

    if chunks:
        try:
            embeddings = _embed(chunks)
        except Exception as exc:  # noqa: BLE001 — embedding is best-effort
            log.warning("embed failed for %s (%r): %s", file_name, username, exc)
            embeddings = []

        if embeddings:
            mem = get_memory()
            # Replace any previous version of this file in the vault first.
            mem.delete_file_chunks(username, file_path)

            ids = [f"{username}:{file_path}:{i}" for i in range(len(chunks))]
            metadatas = [
                {
                    "username": username,
                    "file_path": file_path,
                    "file_name": file_name,
                    "chunk_index": i,
                    "content_hash": content_hash,
                }
                for i in range(len(chunks))
            ]
            mem.upsert_code_chunks(username, ids, embeddings, chunks, metadatas)
            chunk_count = len(chunks)
            log.info("indexed %s → %d chunks into %r's vault.", file_name, chunk_count, username)

    # ── Step 1 (write): persist the relational record with the final count ───
    with get_session() as db:
        db.add(
            FileTracking(
                username=username,
                file_path=file_path,
                file_name=file_name,
                file_content=content,
                content_hash=content_hash,
                chunk_count=chunk_count,
            )
        )

    return {"saved": True, "chunks": chunk_count, "skipped": False}
