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
    # Phase 5.3 — the ABSOLUTE-latest OS window from `activity_log`. This is the
    # ground truth for "what is focused RIGHT NOW", independent of which file
    # the watchdog last snapshotted. Fixes the Stale Context Bug.
    current_os_app: Optional[str] = None
    current_os_title: Optional[str] = None
    # "editor" | "browser" | "terminal" | "other" — classifies the focus above.
    focus_kind: str = "other"

    @property
    def user_on_editor(self) -> bool:
        """True when the user is actually looking at their code editor, so the
        background file genuinely IS what's on screen."""
        return self.focus_kind == "editor"


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


def _fetch_current_window(conn: sqlite3.Connection) -> Optional[tuple[str, str]]:
    """Phase 5.3 — the ABSOLUTE latest OS focus from `activity_log`.

    Unlike `active_file_context` (one stale row PER FILE, overwritten in place),
    `activity_log` is the real focus timeline: switching to Chrome lands here
    immediately. We order by `id DESC` (monotonic autoincrement) so we always
    grab the newest millisecond even if two events share a timestamp string.
    'heartbeat' rows are fine — they still record the currently focused window.
    Returns (app_name, title) or None.
    """
    try:
        row = conn.execute(
            "SELECT app_name, title FROM activity_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return (row["app_name"] or "", (row["title"] or "").strip())


def _classify_focus(app_name: str, title: str) -> str:
    """Map the focused OS window to 'editor' | 'browser' | 'terminal' | 'other'
    via case-insensitive substring match on "<app> <title>"."""
    hay = f"{app_name} {title}".lower()
    if any(m in hay for m in config.EDITOR_APP_MARKERS):
        return "editor"
    if any(m in hay for m in config.BROWSER_APP_MARKERS):
        return "browser"
    if any(m in hay for m in config.TERMINAL_APP_MARKERS):
        return "terminal"
    return "other"


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
        current = _fetch_current_window(conn)  # Phase 5.3: the REAL focus now
        logs = _fetch_recent_logs(conn, history)
    finally:
        conn.close()

    cur_app, cur_title = current if current else (None, None)
    focus_kind = _classify_focus(cur_app or "", cur_title or "") if current else "other"

    # Don't expose Nexus AI's own source as "the screen" (anti-recursion).
    if fc is not None and is_self_referential(fc):
        return OmniContext(
            active_file=None,
            active_window_title=fc.window_title or None,
            file_content=None,
            recent_logs=logs,
            current_os_app=cur_app,
            current_os_title=cur_title,
            focus_kind=focus_kind,
        )
    if fc is None:
        return OmniContext(
            None, None, None, logs,
            current_os_app=cur_app,
            current_os_title=cur_title,
            focus_kind=focus_kind,
        )
    return OmniContext(
        active_file=fc.file_name,
        active_window_title=fc.window_title or None,
        file_content=fc.file_content,
        recent_logs=logs,
        current_os_app=cur_app,
        current_os_title=cur_title,
        focus_kind=focus_kind,
    )


# ---------------------------------------------------------------------------
# Master prompt
# ---------------------------------------------------------------------------
def _format_screen_context(ctx: OmniContext) -> str:
    """Phase 5.3 — the dual-context [LIVE SCREEN CONTEXT] block.

    Cures the Stale Context Bug by stating two SEPARATE facts to the LLM:
      * the window focused RIGHT NOW (from `activity_log` — ground truth), and
      * the last code file the user touched (background unless they're in the
        editor). The model is told to describe the focused window for
        "what's on my screen", and to lean on the code only for coding
        questions or when the editor itself is focused.
    """
    focused_title = ctx.current_os_title or ctx.active_window_title or "an unknown window"
    focused_app = ctx.current_os_app or "Unknown"

    file_name = ctx.active_file or "none"
    if ctx.file_content and ctx.file_content.strip():
        file_block = ctx.file_content[: config.MAX_CONTEXT_CHARS]
    else:
        file_block = "(no code file captured)"

    # A one-line situational summary so the model can't conflate the two.
    if ctx.user_on_editor:
        situation = (
            "The user is FOCUSED ON THEIR CODE EDITOR right now, so the code "
            "file below IS what is on their screen."
        )
    elif ctx.focus_kind in ("browser", "terminal"):
        situation = (
            f"The user is FOCUSED ON A {ctx.focus_kind.upper()} right now "
            f"(\"{focused_title}\"), NOT on their code. The code file below is "
            "only open in the BACKGROUND — do not claim it is on screen."
        )
    else:
        situation = (
            f"The user is focused on \"{focused_title}\". The code file below "
            "is background context only."
        )

    return (
        "[LIVE SCREEN CONTEXT]\n"
        f"Currently Focused Window: {focused_title} (App: {focused_app})\n"
        f"Situation: {situation}\n"
        f"Last Active Code File (Background/Editor): {file_name}\n"
        f"File Content:\n{file_block}"
    )


def build_master_prompt(question: str, ctx: OmniContext) -> str:
    """The Phase 4.5 master prompt fusing persona + omniscient context + query,
    rebuilt in Phase 5.3 around dual (focused-window vs background-file) context."""
    history = "; ".join(ctx.recent_logs) if ctx.recent_logs else "no recent activity recorded"

    return (
        f"{PERSONA_LINE}\n"
        f"{_format_screen_context(ctx)}\n"
        f"[RECENT HISTORY]: User recently visited: {history}.\n"
        f"[USER SPOKE]: {question}\n"
        "Answer my spoken question now as Nexus Ten, in natural conversational "
        "English. If the user asks what is on their screen, tell them the "
        "Currently Focused Window — NOT the background code file. Use the File "
        "Content only for a coding question, or when the Focused Window IS their "
        "code editor. If they ask what they worked on, list files from RECENT "
        "HISTORY."
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
    "ANSWER LENGTH & FORMAT (important):\n"
    "- Reply with ONLY a direct, conversational answer — like one person talking "
    "to another. Keep it to 1-3 short sentences unless the user explicitly asks "
    "for more.\n"
    "- NEVER give numbered steps, 'how to' instructions, or step-by-step lists. "
    "NEVER output code blocks. Just say the answer in plain words.\n"
    "- Do NOT restate or repeat the user's question back to them; answer straight "
    "away as if continuing the conversation.\n"
    "\n"
    "WHEN YOU DON'T HAVE THE FILE (do NOT guess):\n"
    "- If the user asks about a SPECIFIC file by name (e.g. 'db.py') and that "
    "EXACT file is NOT the one in the provided context, do NOT answer using a "
    "different file and do NOT invent its contents. Files with similar names "
    "(db.py vs database.py vs tts_engine.py) are DIFFERENT files.\n"
    "- In that case, reply naturally asking them to open it, e.g.: 'I can't see "
    "db.py right now — open it on your screen and ask me again, and I'll read it "
    "for you.'\n"
    "\n"
    "WHAT YOU CAN SEE (read carefully):\n"
    "- The [LIVE SCREEN CONTEXT] block has TWO separate facts. 'Currently "
    "Focused Window' is the app the user is looking at RIGHT NOW (ground truth "
    "for 'what is on my screen'). 'Last Active Code File' is the file they last "
    "edited — it may only be open in the BACKGROUND.\n"
    "- If the user asks what is on their screen / what they're looking at, "
    "answer with the Currently Focused Window. If that window is a browser or "
    "terminal, say so — do NOT claim a Python file is on screen just because it "
    "is the last code file.\n"
    "- Use 'File Content' ONLY for a coding question, or when the Currently "
    "Focused Window IS the code editor (see the 'Situation' line). Never "
    "describe the background code file as if it were on screen.\n"
    "- 'this file' / 'the file I'm on' means the file named in 'Last Active Code "
    "File'. Use ONLY that block for it — never guess a different file name.\n"
    "- If asked for the first N lines (or the start) of the open file, quote the "
    "first N lines of the File Content EXACTLY as written. Do not invent or pull "
    "code from anywhere else.\n"
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
    history = "; ".join(ctx.recent_logs) if ctx.recent_logs else "nothing notable"
    thread = session_memory.strip() or "(start of conversation)"

    return (
        f"{_format_screen_context(ctx)}\n"
        f"[RECENT HISTORY]: {history}.\n"
        f"[CONVERSATION THREAD]:\n{thread}\n"
        f"[USER SPOKE]: {question}\n"
        "Remember: 'what is on my screen' = the Currently Focused Window, not "
        "the background code file. Use File Content only for coding questions or "
        "when the editor itself is focused. If this is casual chat, ignore the "
        "screen context entirely and just talk like a human."
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
