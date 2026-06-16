# Phase 5.3 — Hybrid Context Retrieval Mixer · Stale Context Bug Fix

> **Status:** Fixed & verified — all 72 tests pass (`python -m unittest tests.test_tracker`).
> **Files:** `tracker/context_engine.py`, `tracker/config.py`, `tests/test_tracker.py`.

## The bug

Ask **"What do you see on my screen?"** while focused on **Google Chrome**, and Nexus
answered with the contents of the last Python file (`ask.py`). It ignored the focus switch
even though the OS tracker had logged it.

**Root cause.** Two tables, two different meanings of "now":

| Table | What it really is |
| --- | --- |
| `activity_log` | The **true OS focus timeline** — append-only, `id` autoincrement. The Chrome switch lands here instantly. |
| `active_file_context` | **One row per file**, `ON CONFLICT(absolute_path) DO UPDATE` (overwritten in place). Its `window_title` is frozen at the moment the file was last *edited*. |

`assemble_context()` derived the "active window" from the **file** row, and
`build_master_prompt()` did `active_file = ctx.active_file or ctx.active_window_title` —
so it *always* preferred the file name. Switching to Chrome never changed the answer
because the code only ever read the stale file row.

## The fix (dual-context fetching)

`tracker/context_engine.py`:

1. **`_fetch_current_window(conn)`** — reads the absolute-latest OS focus from
   `activity_log` with `ORDER BY id DESC LIMIT 1` (monotonic id beats timestamp ties).
   This is the new ground truth for "right now".
2. **`_classify_focus(app, title)`** — case-insensitive substring match against new
   `config.EDITOR_APP_MARKERS` / `BROWSER_APP_MARKERS` / `TERMINAL_APP_MARKERS`, returning
   `"editor" | "browser" | "terminal" | "other"`.
3. **`OmniContext`** gained `current_os_app`, `current_os_title`, `focus_kind`, and a
   `user_on_editor` property. `active_file` / `file_content` now explicitly mean the
   **background** file, never "the screen".
4. **`_format_screen_context()`** renders the new block (shared by both the voice master
   prompt and the session prompt), and a `Situation:` line tells the LLM plainly whether
   the code is on-screen or merely in the background.
5. **`NEXUS_SYSTEM_PROMPT`** rules were rewritten: the old text called the file block
   "ground truth — trust it", which *re-created* the bug. It now teaches the model to use
   the **Currently Focused Window** for "what's on my screen" and the file content only for
   coding questions or when the editor itself is focused.

### Resulting prompt (Chrome focused, `ask.py` in background)

```
[LIVE SCREEN CONTEXT]
Currently Focused Window: GitHub Repository Not Found - Google Chrome (App: Google Chrome)
Situation: The user is FOCUSED ON A BROWSER right now (...), NOT on their code.
           The code file below is only open in the BACKGROUND — do not claim it is on screen.
Last Active Code File (Background/Editor): ask.py
File Content:
import sys
...
```

When VS Code is focused, `Situation` flips to *"FOCUSED ON THEIR CODE EDITOR … the code
file below IS what is on their screen."*

## Logic explanation — why this cures the hallucination

The model was never lying; it was answering the only "screen" fact we gave it — the stale
file. By fetching the **focused window** and the **background file** as two *separately
labelled* facts, and adding an explicit `Situation` verdict from `_classify_focus`, the
prompt no longer conflates them. "What's on my screen?" now binds to *Currently Focused
Window* (Chrome), while the code stays available — clearly tagged as background — for when
a coding question genuinely needs it. The classifier is the hinge: it converts a raw window
title into an instruction the LLM can't misread, so Chrome can never be mistaken for a
Python file again.

## Verify

```bash
python -m unittest tests.test_tracker      # 72 passed
```
