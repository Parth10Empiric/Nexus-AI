"""
retriever.py — Phase 5.3 "Hybrid Context Retrieval Mixer".

The intelligence layer that runs inside the orchestrator's THINKING state. For
each spoken question it:

  1. embeds the question with nomic-embed-text (Ollama),
  2. triple-fetches IN PARALLEL (asyncio.gather):
        • the live active file from SQLite (Phase 3),
        • top-K code chunks from codebase_index (Phase 5.2),
        • top-K logs from activity_memory (Phase 5.2),
  3. RELEVANCE-FILTERS the vector hits by cosine distance — irrelevant hits
     (e.g. for "how are you?") are dropped so casual chat carries no code,
  4. assembles the structured Master Prompt user-content.

The persona + situational-awareness rules live in the system role
(`context_engine.NEXUS_SYSTEM_PROMPT`); this module builds the user content.
Everything degrades gracefully: empty ChromaDB or a locked SQLite just yields
empty sections, never an exception that breaks the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import List

from . import config, context_engine
from .context_engine import OmniContext

log = logging.getLogger("nexus.retriever")


@dataclass
class HybridContext:
    active: OmniContext
    code_hits: List[dict] = field(default_factory=list)
    activity_hits: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Embedding (single query) via local Ollama nomic-embed-text
# ---------------------------------------------------------------------------
def embed_query(text: str,
                ollama_url: str = config.OLLAMA_URL,
                model: str = config.EMBED_MODEL) -> list:
    req = urllib.request.Request(
        f"{ollama_url}/api/embed",
        data=json.dumps({"model": model, "input": [text]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["embeddings"][0]


def _filter_by_distance(hits: List[dict],
                        max_distance: float = config.RAG_MAX_DISTANCE) -> List[dict]:
    """Keep only hits whose cosine distance is within the relevance threshold."""
    kept = [h for h in hits if h.get("distance", 99) <= max_distance]
    if hits and not kept:
        log.info("dropped %d vector hits (all beyond distance %.2f) — casual/off-topic",
                 len(hits), max_distance)
    return kept


# ---------------------------------------------------------------------------
# The non-blocking triple-fetch
# ---------------------------------------------------------------------------
async def retrieve_context(question: str, memory, *,
                           db_path=config.DB_PATH,
                           top_k: int = config.RAG_TOP_K) -> HybridContext:
    """Embed the query, then fetch active-file + code + history concurrently.
    Returns a HybridContext with relevance-filtered vector hits."""
    loop = asyncio.get_running_loop()

    # Embedding must precede the two vector queries (they need the vector).
    # Run it in an executor so the event loop stays free.
    try:
        qvec = await loop.run_in_executor(None, embed_query, question)
    except Exception as exc:
        log.warning("query embed failed (%s) — no vector context this turn", exc)
        qvec = None

    async def fetch_active():
        try:
            return await loop.run_in_executor(
                None, context_engine.assemble_context, db_path)
        except Exception as exc:
            log.warning("active SQLite fetch failed: %s", exc)
            return OmniContext(None, None, None, [])

    async def fetch_code():
        if qvec is None:
            return []
        try:
            return await loop.run_in_executor(
                None, memory.query_codebase, qvec, top_k)
        except Exception as exc:
            log.warning("codebase query failed: %s", exc)
            return []

    async def fetch_activity():
        if qvec is None:
            return []
        try:
            return await loop.run_in_executor(
                None, memory.query_activity, qvec, top_k)
        except Exception as exc:
            log.warning("activity query failed: %s", exc)
            return []

    # THE TRIPLE FETCH — SQLite + ChromaDB code + ChromaDB logs, all in parallel.
    active, code_hits, activity_hits = await asyncio.gather(
        fetch_active(), fetch_code(), fetch_activity())

    # The active file is already shown in full under [ACTIVE SCREEN CONTEXT];
    # drop its own chunks from the "other files" list so the model never
    # confuses a retrieved snippet of it for the current file's real content.
    code_hits = _filter_by_distance(code_hits)
    active_name = getattr(active, "active_file", None)
    if active_name:
        code_hits = [h for h in code_hits
                     if h.get("metadata", {}).get("file_name") != active_name]

    return HybridContext(
        active=active,
        code_hits=code_hits,
        activity_hits=_filter_by_distance(activity_hits),
    )


# ---------------------------------------------------------------------------
# Master prompt assembly (USER role; system role = NEXUS_SYSTEM_PROMPT)
# ---------------------------------------------------------------------------
def _fmt_active(active: OmniContext) -> tuple:
    name = active.active_file or active.active_window_title or "an unknown window"
    code = (active.file_content or "").strip()
    code = code[: config.MAX_CONTEXT_CHARS] if code else "(no readable code captured)"
    return name, code


def _fmt_code_hits(hits: List[dict]) -> str:
    if not hits:
        return "(none relevant to this question)"
    lines = []
    for h in hits:
        meta = h.get("metadata", {})
        fname = meta.get("file_name", "?")
        path = meta.get("path", "")
        snippet = (h.get("document") or "").strip()[:600]
        lines.append(f"- {fname} ({path}):\n{snippet}")
    return "\n".join(lines)


def _fmt_activity_hits(hits: List[dict]) -> str:
    if not hits:
        return "(none relevant to this question)"
    return "\n".join(f"- {(h.get('document') or '').strip()}" for h in hits)


def build_master_user_content(question: str, ctx: HybridContext,
                              chat_turns: str) -> str:
    """The USER-role payload combining all four context blocks + the question."""
    active_file, active_code = _fmt_active(ctx.active)
    return (
        "Answer the user's question. For anything about 'the current/open file' "
        "or 'this file', the file's NAME and real CONTENT are exactly the "
        "[ACTIVE SCREEN CONTEXT] below — use only that. [OTHER PROJECT FILES] "
        "are different files, not the open one. If the question is casual, "
        "ignore all of this and just chat.\n"
        "\n"
        "[ACTIVE SCREEN CONTEXT]  (the file the user currently has OPEN)\n"
        f"Currently open file name: {active_file}\n"
        f"Its full contents:\n{active_code}\n"
        "\n"
        "[OTHER PROJECT FILES]  (relevant elsewhere in the codebase — NOT the open file)\n"
        f"{_fmt_code_hits(ctx.code_hits)}\n"
        "\n"
        "[WORK HISTORY]  (only highly relevant logs)\n"
        f"{_fmt_activity_hits(ctx.activity_hits)}\n"
        "\n"
        "[CONVERSATION HISTORY]\n"
        f"{chat_turns.strip() or '(start of conversation)'}\n"
        "\n"
        f"[USER SPOKE]: {question}"
    )


async def retrieve_and_build(question: str, memory, chat_turns: str,
                             db_path=config.DB_PATH) -> str:
    """One call: triple-fetch + filter + assemble the master user content."""
    ctx = await retrieve_context(question, memory, db_path=db_path)
    return build_master_user_content(question, ctx, chat_turns)
