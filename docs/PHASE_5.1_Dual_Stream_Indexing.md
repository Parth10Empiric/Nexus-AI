# Nexus AI — Phase 5.1: Dual-Stream Vector Indexing (ChromaDB RAG)

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** code-aware chunking + two separate collections work.

---

## 1. What is this phase, in one sentence?

We gave Nexus two kinds of searchable long-term memory in a local vector store:
the **whole codebase** (so it can answer about any file, not just the open one)
and a **chronological activity history** (so it can recall what you did and
when) — both embedded locally, nothing leaves the machine.

---

## 2. The two streams

```
  Stream A: ~/Projects/EMPIRA_HR/**.py,.html,.js
       │  RecursiveCharacterTextSplitter (code-aware)
       │  nomic-embed-text (Ollama)         ┌───────────────────────┐
       └──────────────────────────────────▶│ codebase_index        │
                                            └───────────────────────┘
  Stream B: local_logs.db (activity_log)    ┌───────────────────────┐
       │  "On <ts>, user worked in <app> on <title>"
       │  nomic-embed-text (Ollama)         │ activity_memory       │
       └──────────────────────────────────▶└───────────────────────┘
                                            ChromaDB (./chroma_db, persistent)
```

Verified end-to-end: a query "how do we verify a user's login password?" ranked
`auth.py` first; "when was I debugging the token problem?" ranked the JWT /
Stack Overflow log first.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **ChromaDB** (persistent) | Local, file-backed vector DB. Two named collections keep code and history isolated. |
| **`langchain-text-splitters`** | `RecursiveCharacterTextSplitter.from_language()` splits along code structure, not arbitrary characters. |
| **`nomic-embed-text` via Ollama** | Local embedding model (768-dim). Same model for both streams so vectors are comparable. |
| **read-only SQLite** | Stream B reads `activity_log` without disturbing the tracker writer. |

---

## 4. Code-aware chunking — why functions aren't cut in half

A naive splitter cuts every N characters, which routinely slices a function
down the middle — half its body lands in one chunk, half in another, and
neither embeds to anything meaningful.

`RecursiveCharacterTextSplitter.from_language(Language.PYTHON, …)` instead uses
an **ordered list of Python-aware separators** — it tries to split on
`\nclass `, then `\ndef `, then `\n\n`, then single newlines, and only falls
back to raw characters as a last resort. It applies them **recursively**: it
breaks the text at the highest-level boundary that keeps chunks under the size
limit, so a chunk boundary lands *between* functions/classes rather than inside
one. With `chunk_size≈2000` chars (~500 tokens) and `200`-char (~50-token)
overlap, each chunk is a coherent unit of code, and the overlap means a
reference near a boundary still appears whole in at least one chunk. We pick the
separator set per extension (`.py`→PYTHON, `.html`→HTML, `.js`→JS). Verified: a
small whole function stays a single chunk.

---

## 5. Why two collections (not one) — preventing hallucination

`codebase_index` and `activity_memory` are kept **physically separate** because
they answer fundamentally different questions and must never be confused:

- **Different meaning of a match.** A hit in `codebase_index` is *"this code
  exists in the project."* A hit in `activity_memory` is *"this happened at this
  time."* If they shared one collection, a semantic search for "auth" could
  return a log line ("user viewed auth.py at 2pm") interleaved with actual
  source, and the LLM might quote a *log sentence as if it were code* — a
  hallucination born of mixed retrieval.
- **Query routing.** "What does the login function do?" should retrieve **code**;
  "what was I doing yesterday?" should retrieve **history**. Separate
  collections let the retriever query the right store and keep the prompt clean,
  so the model isn't handed irrelevant rows that dilute its attention.
- **Independent lifecycles.** Code chunks are replaced on file save (Stream A);
  log entries only ever accumulate (Stream B). Separate collections let each be
  updated, capped, or cleared on its own schedule.

Keeping them apart is the retrieval-side equivalent of the Phase 4.5
"situational awareness" fix: give the model only the *kind* of context the
question actually needs.

---

## 5b. Active-file-only indexing, dedupe & desktop connection (update)

The indexer was upgraded so it never re-does work and stays connected to the
rest of the app:

- **Shared store.** `VectorIndexer` now writes through the Phase 5.2
  `NexusMemoryManager` singleton (not its own client), so everything it indexes
  is exactly what the Phase 5.3 retriever reads. One vault, one connection.
- **Active-file only.** `index_active_file()` indexes *just the file the
  developer currently has open* (read from the `active_file_context` table) —
  no full-project rescan — and skips Nexus AI's own source.
- **Content-hash dedupe.** Each file's chunks store a `content_hash`. Before
  embedding, `index_single_file()` compares the current hash to the stored one;
  if unchanged, it **skips entirely** (no redundant embedding). Verified: a
  second index of an unchanged file returns 0; editing it re-indexes.
- **Auto-index on save.** With `AUTO_INDEX_ON_SAVE = True`, the Phase 3.2
  watchdog calls `index_single_file()` on every Ctrl+S (via an `on_saved` hook),
  so the vault tracks your edits live — deduped, so it's free when nothing
  changed.
- **One launcher connects everything to the desktop app.** `run_nexus.py`
  starts the Tracker daemon (eyes + watchdog + auto-index) and the Session
  Orchestrator (brain + voice + the `ws://127.0.0.1:8765` bridge) in one
  process sharing the one vault; the Tauri/React dashboard connects to that
  bridge. `python run_nexus.py` then `npm run tauri:dev`.

## 6. Watchdog integration — live updates on Ctrl+S

`index_single_file(filepath)` is the modular hook the Phase 3.2 `watchdog`
daemon can call on every save. It:
1. rejects non-source / ignored paths,
2. reads the file with the shared guardrails (size/binary),
3. re-chunks it,
4. **deletes the file's old chunks** (`where={"path": abs_path}`) then upserts
   the new ones — so an edit updates the index in place instead of duplicating.

Deterministic ids (`"{abs_path}::{chunk_index}"`) make this idempotent. Verified:
after editing `auth.py` to use 2FA, a query for "2FA" returned the new code, and
the chunk count stayed constant (replaced, not piled up). To wire it, call
`indexer.index_single_file(path)` from the observer's save handler.

Thread safety: every Chroma write is guarded by a lock, so the watchdog thread
and a full `index_project()` can't corrupt each other.

---

## 7. Files in this phase

```
tracker/
├── vector_indexer.py    # NEW — VectorIndexer: dual streams, query, watchdog hook
└── config.py            # UPDATED — CHROMA_DIR, collections, EMBED_MODEL, chunking
requirements.txt          # UPDATED — langchain-text-splitters
tests/test_tracker.py     # UPDATED — indexer mechanics (59 total, all pass)
chroma_db/                # the persistent vector store (created on first run)
```

---

## 8. How to run

```bash
pip install -r requirements.txt
ollama pull nomic-embed-text          # one-time

# Full index of a project + the activity logs:
python -m tracker.vector_indexer ~/Projects/EMPIRA_HR

# In code:
from tracker.vector_indexer import VectorIndexer
idx = VectorIndexer()
idx.index_project("~/Projects/EMPIRA_HR")
idx.index_activity_logs()
idx.query_code("where is JWT validated?")        # → ranked code chunks
idx.query_activity("what did I do this morning?") # → ranked log entries
```

---

## 9. What's next (Phase 5.2)

The retriever feeds these collections into the prompt: on a code question, pull
the top-k `codebase_index` chunks; on a history question, pull top-k
`activity_memory` entries — turning Nexus's "omniscient context" from just the
*active* file into the *entire project + full timeline*.
