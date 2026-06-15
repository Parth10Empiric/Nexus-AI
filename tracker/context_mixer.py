"""
context_mixer.py — Phase 3.3 "Automated Prompt Context Mixer".

The bridge between the live `active_file_context` table and the local LLM.
Given the developer's chat question, it:

    1. loads the most recent active-file snapshot from local_logs.db,
    2. applies the CRITICAL "Nexus AI" self-exclusion guard (so the assistant
       never ingests its own source and loops),
    3. builds a structured system prompt that wraps the code in a fenced block
       with metadata, OR falls back to a clean prompt with no injection.

It does NOT talk to Ollama itself — it returns the finished system prompt so
the caller (the React chat via a Tauri command, or a Python hotkey handler in
Phase 2.2) can stream the request. Pure, fast (sub-millisecond), and trivially
testable.

This is the canonical reference implementation. The Tauri/Rust command and the
JS mixer in the frontend mirror this exact logic for the desktop chat path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
# Clean fallback: used when there is no usable external file context (no file,
# or the active file is Nexus AI's own code).
CLEAN_SYSTEM_PROMPT = (
    "You are Nexus AI, an elite senior software engineer assisting the "
    "developer. Answer concisely and practically. Output code in fenced "
    'blocks. Do not say "Sure, here is".'
)

# Map a file extension to a Markdown code-fence language hint.
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
    ".tsx": "tsx", ".rs": "rust", ".go": "go", ".java": "java", ".rb": "ruby",
    ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp",
    ".html": "html", ".css": "css", ".scss": "scss", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sql": "sql",
    ".sh": "bash", ".md": "markdown", ".vue": "vue", ".kt": "kotlin",
}


@dataclass(frozen=True)
class FileContext:
    file_name: str
    absolute_path: str
    file_content: str
    window_title: str = ""


# ---------------------------------------------------------------------------
# 1 + 2. Load + self-exclusion guard
# ---------------------------------------------------------------------------
def load_active_context(db_path: Path = config.DB_PATH) -> Optional[FileContext]:
    """Read the latest active_file_context row. Returns None if the table is
    empty or unreadable. Uses its own short-lived connection so it is safe to
    call from any thread/process."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT window_title, file_name, absolute_path, file_content "
            "FROM active_file_context ORDER BY ts_utc DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if row is None:
        return None
    return FileContext(
        file_name=row["file_name"],
        absolute_path=row["absolute_path"],
        file_content=row["file_content"],
        window_title=row["window_title"] or "",
    )


def is_self_referential(ctx: FileContext) -> bool:
    """
    The infinite-loop guardrail. True if this context belongs to Nexus AI's
    OWN codebase and must therefore be excluded from injection.

    Two independent checks (either is sufficient):
      a) the absolute path lives inside this project's root directory, or
      b) any self-exclude marker ("nexus ai", "nexus_ai", …) appears in the
         path or the window title.
    """
    # Honour the global switch: when self-context is allowed (e.g. you ARE
    # developing Nexus AI and want to ask about its own files), never exclude.
    if not config.EXCLUDE_SELF_CONTEXT:
        return False

    # (a) Robust structural check: is the file under our own project root?
    if ctx.absolute_path:
        try:
            target = Path(ctx.absolute_path).resolve()
            root = config.ROOT_DIR.resolve()
            if target == root or root in target.parents:
                return True
        except (OSError, ValueError, RuntimeError):
            pass  # fall through to string matching

    # (b) Defensive string check on path + title (handles symlinks, odd mounts).
    haystack = f"{ctx.absolute_path} {ctx.window_title}".lower()
    return any(marker in haystack for marker in config.SELF_EXCLUDE_MARKERS)


# ---------------------------------------------------------------------------
# 3. Prompt engineering / formatting
# ---------------------------------------------------------------------------
def _lang_hint(file_name: str) -> str:
    return _LANG_BY_EXT.get(Path(file_name).suffix.lower(), "")


def _truncate(content: str) -> str:
    """Keep the head of the file (imports/definitions); note any truncation."""
    limit = config.MAX_CONTEXT_CHARS
    if len(content) <= limit:
        return content
    dropped = len(content) - limit
    return content[:limit] + f"\n\n# … [truncated {dropped} characters] …"


def build_system_prompt(ctx: Optional[FileContext]) -> str:
    """
    The mixer's output. Returns the structured code-context system prompt when
    a valid EXTERNAL file is active, otherwise the clean fallback prompt.
    """
    if ctx is None or not ctx.file_content.strip() or is_self_referential(ctx):
        return CLEAN_SYSTEM_PROMPT

    lang = _lang_hint(ctx.file_name)
    code = _truncate(ctx.file_content)

    return (
        "You are an elite senior software engineer reviewing the developer's "
        "active workspace.\n"
        f"Current Open File: {ctx.file_name}\n"
        f"Absolute Path: {ctx.absolute_path}\n\n"
        "[ACTIVE CODE CONTEXT]\n"
        f"```{lang}\n{code}\n```\n\n"
        "Answer the user's question concisely based on the code context "
        "provided above. Prioritize providing clean, optimal code snippets "
        'over verbose explanations. Do not say "Sure, here is the fix".'
    )


# ---------------------------------------------------------------------------
# Convenience: full mix in one call
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MixedPayload:
    system: str
    prompt: str
    injected: bool          # True if real code context was injected
    file_name: Optional[str]


def mix(question: str, db_path: Path = config.DB_PATH) -> MixedPayload:
    """One-shot helper: load context, apply guard, build the payload for
    /api/generate. The `prompt` is the raw user question; `system` carries the
    (possibly code-injected) instructions."""
    ctx = load_active_context(db_path)
    system = build_system_prompt(ctx)
    injected = ctx is not None and not is_self_referential(ctx) and bool(
        ctx.file_content.strip()
    )
    return MixedPayload(
        system=system,
        prompt=question,
        injected=injected,
        file_name=ctx.file_name if (ctx and injected) else None,
    )
