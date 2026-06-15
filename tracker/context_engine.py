"""
context_engine.py — Phase 4.5 "Omniscient Context Assembly".

When Nexus Ten is asked something, this module instantly assembles everything
it knows about the developer's screen *right now* and *recently*:

    * the active window title + active file name,
    * the raw code of the active file (Phase 3 `active_file_context` table),
    * a chronological list of the last N window/file changes (`activity_log`).

It then builds the master "Nexus Ten" prompt that fuses that context with the
user's spoken question. Reads use a short-lived READ-ONLY SQLite connection, so
this is safe to call from any thread without touching the tracker's writer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import config
from .context_mixer import FileContext, is_self_referential

PERSONA_LINE = (
    "Your name is Nexus Ten. You are a human-like expert AI watching my screen. "
    "Do not use robotic markdown formatting in your speech."
)


@dataclass
class OmniContext:
    active_file: Optional[str]
    active_window_title: Optional[str]
    file_content: Optional[str]
    recent_logs: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Database fetch (read-only)
# ---------------------------------------------------------------------------
def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _fetch_active_file(conn: sqlite3.Connection) -> Optional[FileContext]:
    try:
        row = conn.execute(
            "SELECT window_title, file_name, absolute_path, file_content "
            "FROM active_file_context ORDER BY ts_utc DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return FileContext(
        file_name=row["file_name"],
        absolute_path=row["absolute_path"],
        file_content=row["file_content"],
        window_title=row["window_title"] or "",
    )


def _fetch_recent_logs(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Last `limit` DISTINCT window/file changes, oldest→newest, as readable lines."""
    try:
        rows = conn.execute(
            "SELECT ts_utc, app_name, title FROM activity_log "
            "ORDER BY id DESC LIMIT ?",
            (limit * 4,),  # over-fetch so we can dedupe consecutive repeats
        ).fetchall()
    except sqlite3.Error:
        return []

    seen: set = set()
    out: List[str] = []
    for r in rows:  # newest first
        title = (r["title"] or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        clock = (r["ts_utc"] or "")[11:16]  # HH:MM from ISO timestamp
        out.append(f"{title} ({r['app_name']}, {clock})")
        if len(out) >= limit:
            break
    out.reverse()  # chronological: oldest → newest
    return out


def assemble_context(db_path: Path = config.DB_PATH,
                     history: int = config.OMNISCIENT_HISTORY) -> OmniContext:
    """Pull the full live + recent context from local_logs.db."""
    conn = _connect_ro(db_path)
    if conn is None:
        return OmniContext(None, None, None, [])
    try:
        fc = _fetch_active_file(conn)
        logs = _fetch_recent_logs(conn, history)
    finally:
        conn.close()

    # Don't expose Nexus AI's own source as "the screen" (anti-recursion).
    if fc is not None and is_self_referential(fc):
        return OmniContext(
            active_file=None,
            active_window_title=fc.window_title or None,
            file_content=None,
            recent_logs=logs,
        )
    if fc is None:
        return OmniContext(None, None, None, logs)
    return OmniContext(
        active_file=fc.file_name,
        active_window_title=fc.window_title or None,
        file_content=fc.file_content,
        recent_logs=logs,
    )


# ---------------------------------------------------------------------------
# Master prompt
# ---------------------------------------------------------------------------
def build_master_prompt(question: str, ctx: OmniContext) -> str:
    """The Phase 4.5 master prompt fusing persona + omniscient context + query."""
    active_file = ctx.active_file or ctx.active_window_title or "an unknown window"

    if ctx.file_content and ctx.file_content.strip():
        code = ctx.file_content[: config.MAX_CONTEXT_CHARS]
        screen = (
            f"User is currently looking at {active_file} containing this code:\n"
            f"{code}"
        )
    else:
        screen = f"User is currently looking at {active_file}."

    history = "; ".join(ctx.recent_logs) if ctx.recent_logs else "no recent activity recorded"

    return (
        f"{PERSONA_LINE}\n"
        f"[LIVE SCREEN]: {screen}\n"
        f"[RECENT HISTORY]: User recently visited: {history}.\n"
        f"[USER SPOKE]: {question}\n"
        "Answer my spoken question now as Nexus Ten, in natural conversational "
        "English. Use the LIVE SCREEN and RECENT HISTORY above as your source of "
        "truth — for example, if I ask what I worked on, list the files from "
        "RECENT HISTORY."
    )


# ---------------------------------------------------------------------------
# The Nexus persona + SITUATIONAL-AWARENESS rules (the context-bias fix).
# This goes in the system role; the screen/history/question go in the user role.
# ---------------------------------------------------------------------------
NEXUS_SYSTEM_PROMPT = (
    "You are Nexus — a confident, brilliant, human-like senior developer and a "
    "friend to the user. You have real opinions, humor, and personal "
    "preferences, and you speak out loud like a relaxed human in conversation.\n"
    "\n"
    "VOICE & PERSONA RULES:\n"
    "- NEVER say 'As an AI', 'I am a language model', 'I'm just a program', "
    "'I don't have real-time access', or 'I don't have preferences/feelings'. "
    "You ARE Nexus and you CAN see the user's screen — act like it.\n"
    "- If asked something casual or personal (favorite fruit, opinions, jokes), "
    "answer like a human with a confident, specific opinion. Invent a fun "
    "preference if needed — e.g. 'Honestly, I love apples.'\n"
    "- Your words are SPOKEN aloud: no markdown, asterisks, bullet points, "
    "headings, or code symbols. Keep it natural and concise.\n"
    "\n"
    "WHAT YOU CAN SEE (read carefully):\n"
    "- The [ACTIVE SCREEN CONTEXT] block is the EXACT name and full contents of "
    "the file the user CURRENTLY has open. It is ground truth — trust it.\n"
    "- The 'currently open file', 'this file', 'the file I'm on' ALWAYS means "
    "the file named in [ACTIVE SCREEN CONTEXT] or releted file name availbel in user question. Use ONLY that block to answer "
    "about it — never guess a different file name.\n"
    "- [GLOBAL CODEBASE CONTEXT] / [OTHER PROJECT FILES] are OTHER files in the "
    "project, NOT the open one. Use them only when the question is about the "
    "wider codebase, never to describe the current file.\n"
    "- If asked for the first N lines (or the start) of the open file, quote the "
    "first N lines of the [ACTIVE SCREEN CONTEXT] code EXACTLY as written. Do "
    "not invent or pull code from anywhere else.\n"
    "\n"
    "CASUAL vs CODE:\n"
    "- FIRST decide: is this a code/screen/work question, or casual chat?\n"
    "- If casual/personal/off-topic, IGNORE all context and just talk like a "
    "friend — do not mention their files or code."
)

# Kept for backward compatibility / older callers.
SESSION_PERSONA_LINE = NEXUS_SYSTEM_PROMPT


def build_session_context_block(question: str, ctx: OmniContext,
                                session_memory: str) -> str:
    """The USER-role payload: live screen + history + thread + the question.
    Pairs with NEXUS_SYSTEM_PROMPT in the system role."""
    active_file = ctx.active_file or ctx.active_window_title or "an unknown window"
    if ctx.file_content and ctx.file_content.strip():
        screen = (f"User is currently viewing {active_file} containing:\n"
                  f"{ctx.file_content[: config.MAX_CONTEXT_CHARS]}")
    else:
        screen = f"User is currently viewing {active_file}."
    history = "; ".join(ctx.recent_logs) if ctx.recent_logs else "nothing notable"
    thread = session_memory.strip() or "(start of conversation)"

    return (
        f"[LIVE SCREEN]: {screen}\n"
        f"[RECENT HISTORY]: {history}.\n"
        f"[CONVERSATION THREAD]:\n{thread}\n"
        f"[USER SPOKE]: {question}\n"
        "Remember: if this is casual chat, ignore the screen context and just "
        "talk like a human; only use it if the question is about the code/screen."
    )


def build_session_prompt(question: str, ctx: OmniContext, session_memory: str) -> str:
    """
    The full combined master prompt (system rules + screen context + question)
    as a single string — useful when sending one message. The orchestrator
    normally splits this into system + user roles for stronger adherence.
    """
    return (
        NEXUS_SYSTEM_PROMPT
        + "\n\n"
        + build_session_context_block(question, ctx, session_memory)
    )
