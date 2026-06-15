# Nexus AI — Phase 3.3: Automated Prompt Context Mixer

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** the self-exclusion guard and prompt structure work.

---

## 1. What is this phase, in one sentence?

We built the **bridge** that, every time you ask the chat a question, silently
pulls the live code from `active_file_context`, refuses to inject Nexus AI's
own source, and wraps the code in a perfectly structured prompt for the local
model — so the AI debugs the real file on your screen without you pasting
anything.

Phase 2.1 injected only the window *title*. Phase 3.3 injects the actual
*source code* (captured fresh by Phase 3.1/3.2), with a critical safety guard.

---

## 2. The flow

```
  user types a question in the chat
            │
            ▼
  streamGenerate()  (ollama.js)
            │  invoke("get_active_file_context")   ← sub-ms SQLite read
            ▼
  Rust command  reads latest active_file_context row
            │  is_self_referential(path,title)?
            ├── YES → return None  ───────────────┐
            └── NO  → return {file_name, path, content}
                              │                    │
                              ▼                    ▼
              buildSystemPrompt(fileCtx)   CLEAN_SYSTEM_PROMPT (no injection)
                              │
                              ▼
              POST /api/generate  {system, prompt, stream:true}  → typed-out answer
```

The Python `context_mixer.py` implements the identical logic for the
Python-side callers (Phase 2.2 hotkey, Phase 4 reports) and is the tested
reference.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **Tauri command + `rusqlite`** | Reads the latest context row natively in <1ms — no network, no Python process needed for the desktop chat path. |
| **`sqlite3` (Python, read-only)** | The canonical `context_mixer.py` opens the DB `mode=ro` so the mixer can never corrupt the tracker's data. |
| **Plain string building** | The prompt is just a formatted string — zero heavy work, keeping total mix latency far under the 5ms budget (measured 0.4ms). |

---

## 4. The infinite-loop guardrail (the critical requirement)

If the active file is part of **Nexus AI's own codebase**, injecting it would
make the assistant read its own tracker loops, DB code, and logs — confusing,
wasteful, and potentially self-amplifying. `is_self_referential()` blocks this
with two independent checks (either one is enough):

1. **Structural path check.** Resolve the file's absolute path and test whether
   it lives inside this project's root (`config.ROOT_DIR`). This is the most
   reliable signal and is immune to naming tricks.
2. **Marker string check.** Lower-case the path + window title and look for any
   of `nexus ai`, `nexus_ai`, `nexus-ai`. This catches symlinks, odd mounts,
   and copies outside the root.

When it fires, the mixer returns the **clean fallback prompt** (general
assistant, no code) instead of injecting. **Verified:** the real DB's latest
row (`Nexus_AI.md`, inside the project) was correctly excluded; `EMPIRA_HR`
files were injected; and `/opt/nexus_ai/…`, `/x/NEXUS-AI/…` variants were all
caught.

---

## 5. Prompt engineering — keeping a 1.5B model focused

Small models are easily distracted, so the prompt is deliberately structured:

```
You are an elite senior software engineer reviewing the developer's active workspace.
Current Open File: {file_name}
Absolute Path: {absolute_path}

[ACTIVE CODE CONTEXT]
```{language}
{file_content}
```

Answer the user's question concisely based on the code context provided above.
Prioritize providing clean, optimal code snippets over verbose explanations.
Do not say "Sure, here is the fix".
```

Why each part matters:

- **Role first** ("elite senior engineer reviewing…") sets behaviour before
  any data, anchoring the model's persona.
- **Metadata lines** (file name + path) give the model an unambiguous anchor it
  can name in its answer, reducing "which file?" confusion.
- **A labelled, fenced block** (`[ACTIVE CODE CONTEXT]` + ```` ```lang ````)
  creates a hard visual/structural boundary so the model treats the code as
  *data to analyse*, not instructions to follow. The language hint also nudges
  syntax-aware reasoning.
- **Instructions placed AFTER the code** exploit recency: the last thing the
  model reads is *what to do*, which a small model weights heavily.
- **Explicit anti-filler rule** (`Do not say "Sure, here is the fix"`) trims the
  chatty preambles 1.5B models love, keeping answers compact.
- **Head-truncation at 6,000 chars** (with a `[truncated N characters]` note)
  keeps the context inside the model's attention sweet-spot and bounds latency;
  the head of a file (imports, class/function defs) carries the most signal.

**Verified end-to-end:** seeded an external `auth.py` with an unchecked
division, asked "what is the bug?", and the model answered *"does not check if
`b` is zero … `ZeroDivisionError`"* — using only the injected code.

---

## 6. Non-blocking, <5ms delivery

- The context read is a single indexed-row SQLite query (Rust or Python) →
  sub-millisecond. Measured Python `mix()` average: **0.40 ms** over 200 runs.
- Prompt construction is pure string formatting — no I/O, no network.
- Streaming is unchanged: `stream: true` to `/api/generate`, tokens parsed as
  they arrive (Phase 2.1), so the UI never blocks.

---

## 7. Files in this phase

```
tracker/
├── context_mixer.py     # NEW — canonical mixer: load + guard + build prompt
├── db.py                # UPDATED — latest_file_context_full() (incl. content)
└── config.py            # UPDATED — SELF_EXCLUDE_MARKERS, MAX_CONTEXT_CHARS
frontend/
├── src-tauri/src/main.rs  # UPDATED — get_active_file_context command + guard
└── src/lib/ollama.js      # UPDATED — JS mixer + fetchActiveFileContext()
tests/test_tracker.py      # UPDATED — 5 mixer tests (20 total, all pass)
```

---

## 8. How to run

```bash
./run_tracker.sh                # keeps active_file_context fresh (Phase 3.1/3.2)
cd frontend && npm run tauri:dev
# Open a file in an EXTERNAL project (e.g. EMPIRA_HR), then ask the chat
# "explain this function" — the AI answers about your real code.
# Open a Nexus AI file and it falls back to the general assistant (no loop).
```

---

## 9. What's next

The mixer is the shared contract for context. Phase 2.2 (global hotkey) can
call `context_mixer.mix()` directly from Python; Phase 2.3 embeds the same
context into ChromaDB for "what was I working on?" recall.
