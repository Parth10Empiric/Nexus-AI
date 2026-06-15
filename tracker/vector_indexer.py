"""
vector_indexer.py — Phase 5.1/5.3 codebase + activity indexer.

Chunks source files, embeds them locally with nomic-embed-text (Ollama), and
stores the vectors in the SHARED NexusMemoryManager (Phase 5.2) — the same
store the retriever (Phase 5.3) reads, so indexing and retrieval are connected.

Key behaviours (per the desktop-app integration requirements):
  * index_active_file()  — index ONLY the file the developer currently has open
                           (from the Phase 3 active_file_context table).
  * content-hash dedupe  — if a file's content is unchanged since it was last
                           embedded, it is SKIPPED (no redundant embedding).
  * index_single_file()  — the watchdog hook for re-indexing one file on save.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from . import config
from .context_mixer import is_self_referential, load_active_context
from .file_resolver import is_path_ignored, read_text_with_guardrails
from .memory_manager import NexusMemoryManager, get_memory

log = logging.getLogger("nexus.tracker.indexer")

_EXT_LANGUAGE = {
    ".py": Language.PYTHON,
    ".html": Language.HTML,
    ".js": Language.JS,
}


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


class VectorIndexer:
    def __init__(
        self,
        memory: Optional[NexusMemoryManager] = None,
        ollama_url: str = config.OLLAMA_URL,
        embed_model: str = config.EMBED_MODEL,
    ) -> None:
        self._ollama_url = ollama_url
        self._embed_model = embed_model
        # Share the one process-wide vault so the retriever sees what we index.
        self._mem = memory or get_memory()
        self._splitters: dict = {}

    # ------------------------------------------------------------------ #
    # Embeddings + chunking
    # ------------------------------------------------------------------ #
    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        req = urllib.request.Request(
            f"{self._ollama_url}/api/embed",
            data=json.dumps({"model": self._embed_model, "input": texts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        embeddings = data.get("embeddings")
        if not embeddings or len(embeddings) != len(texts):
            raise RuntimeError("embedding count mismatch from Ollama")
        return embeddings

    def _splitter(self, ext: str) -> RecursiveCharacterTextSplitter:
        if ext not in self._splitters:
            lang = _EXT_LANGUAGE.get(ext)
            if lang is not None:
                self._splitters[ext] = RecursiveCharacterTextSplitter.from_language(
                    language=lang, chunk_size=config.CHUNK_CHARS,
                    chunk_overlap=config.CHUNK_OVERLAP_CHARS)
            else:
                self._splitters[ext] = RecursiveCharacterTextSplitter(
                    chunk_size=config.CHUNK_CHARS,
                    chunk_overlap=config.CHUNK_OVERLAP_CHARS)
        return self._splitters[ext]

    # ------------------------------------------------------------------ #
    # Index a single file (with dedupe) — the watchdog hook
    # ------------------------------------------------------------------ #
    def index_single_file(self, filepath: str, force: bool = False) -> int:
        """
        (Re)index ONE file. Returns chunk count, 0 if skipped/ignored.
        If the file's content is unchanged since last index, it is SKIPPED
        (unless force=True) — no redundant embedding.
        """
        path = Path(filepath)
        abs_path = str(path.resolve())
        ext = path.suffix.lower()

        if ext not in config.CODE_EXTENSIONS or is_path_ignored(path):
            return 0
        content = read_text_with_guardrails(path)
        if content is None or not content.strip():
            self._mem.delete_file(abs_path)   # gone/empty → drop stale chunks
            return 0

        new_hash = _hash(content)
        if not force and self._mem.get_file_hash(abs_path) == new_hash:
            log.info("skip %s — already embedded (unchanged)", path.name)
            return 0

        chunks = self._splitter(ext).split_text(content)
        if not chunks:
            return 0
        try:
            embeddings = self._embed(chunks)
        except Exception as exc:
            log.warning("embed failed for %s: %s", abs_path, exc)
            return 0

        self._mem.delete_file(abs_path)       # replace old version atomically-ish
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            self._mem.upsert_code_chunk(
                abs_path, chunk, emb, chunk_index=i, content_hash=new_hash)
        log.info("indexed %s → %d chunks", path.name, len(chunks))
        return len(chunks)

    # ------------------------------------------------------------------ #
    # Index ONLY the currently open/active file (the requested behaviour)
    # ------------------------------------------------------------------ #
    def index_active_file(self, db_path: Path = config.DB_PATH,
                          force: bool = False) -> int:
        """
        Index just the file the developer currently has open (from the Phase 3
        active_file_context table). Skips Nexus AI's own source, and skips
        re-embedding if the file is unchanged. This is the on-demand,
        single-file path — no whole-project scan.
        """
        ctx = load_active_context(db_path)
        if ctx is None or not ctx.absolute_path:
            log.info("no active file to index.")
            return 0
        if is_self_referential(ctx):
            log.info("active file is Nexus AI's own code — skipping.")
            return 0
        return self.index_single_file(ctx.absolute_path, force=force)

    # ------------------------------------------------------------------ #
    # Full project scan (optional bulk seed — still dedupes per file)
    # ------------------------------------------------------------------ #
    def index_project(self, root: str = config.INDEX_PROJECT_ROOT) -> dict:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            log.warning("project root not found: %s", root_path)
            return {"files": 0, "chunks": 0, "skipped": 0}
        files = chunks = skipped = 0
        for dirpath, dirnames, filenames in os.walk(root_path, topdown=True):
            dirnames[:] = [d for d in dirnames
                           if d not in config.IGNORED_DIRS and not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() not in config.CODE_EXTENSIONS:
                    continue
                n = self.index_single_file(str(Path(dirpath) / name))
                if n:
                    files += 1
                    chunks += n
                else:
                    skipped += 1
        log.info("project indexed: %d files, %d chunks (%d skipped/unchanged)",
                 files, chunks, skipped)
        return {"files": files, "chunks": chunks, "skipped": skipped}

    # ------------------------------------------------------------------ #
    # Activity log indexer (Stream B)
    # ------------------------------------------------------------------ #
    def index_activity_logs(self, db_path: Path = config.DB_PATH,
                            limit: int = 2000) -> int:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            log.warning("cannot open %s: %s", db_path, exc)
            return 0
        try:
            rows = conn.execute(
                "SELECT id, ts_utc, app_name, title FROM activity_log "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()

        stored = 0
        for r in rows:
            sentence = (f"On {r['ts_utc']}, the user was working in "
                        f"{r['app_name']} on \"{r['title']}\".")
            try:
                emb = self._embed([sentence])[0]
            except Exception as exc:
                log.warning("activity embed failed: %s", exc)
                continue
            self._mem.upsert_activity_log(
                r["ts_utc"], sentence, emb,
                app_name=r["app_name"], title=r["title"], log_id=f"log-{r['id']}")
            stored += 1
        log.info("activity memory indexed: %d entries", stored)
        return stored

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        return self._mem.stats()


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%H:%M:%S")
    idx = VectorIndexer()
    mode = sys.argv[1] if len(sys.argv) > 1 else "active"
    if mode == "active":
        print("indexing active file…", idx.index_active_file())
    elif mode == "project":
        root = sys.argv[2] if len(sys.argv) > 2 else config.INDEX_PROJECT_ROOT
        print("indexing project:", root, idx.index_project(root))
        print("activity:", idx.index_activity_logs())
    print("stats:", idx.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
