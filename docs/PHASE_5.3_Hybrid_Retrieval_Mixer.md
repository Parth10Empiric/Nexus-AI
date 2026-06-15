# Nexus AI — Phase 5.3: The Hybrid Context Retrieval Mixer

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** the distance threshold turns Nexus from chatbot to code agent.

---

## 1. What is this phase, in one sentence?

On every spoken question, Nexus now fetches three context sources **in
parallel** — the live screen, the whole-project codebase, and the work history
— keeps only the parts mathematically relevant to *that* question, and folds
them into one Master Prompt — so it answers code questions with deep context
and casual questions like a normal human.

---

## 2. The flow inside STATE_THINKING

```
  transcribed question
        │  embed with nomic-embed-text (Ollama)
        ▼
   ┌──────────── asyncio.gather (parallel) ────────────┐
   │  SQLite active file   ChromaDB code   ChromaDB logs │
   └───────────────────────┬───────────────────────────┘
                            ▼  distance filter (drop hits > 0.55)
        ┌───────────────────────────────────────────┐
        │ [ACTIVE SCREEN] [GLOBAL CODE] [HISTORY]     │
        │ [CONVERSATION] [USER SPOKE]   ← Master Prompt│
        └───────────────────────────────────────────┘
                            ▼
                 Ollama → Piper (TTS)
```

Verified: "how is the login authenticated?" kept `auth.py` (distance 0.34) and
dropped the rest; "how are you doing today?" dropped **all** vector context.

---

## 3. Tools & why

| Tool | Role |
|---|---|
| **`nomic-embed-text`** (Ollama) | Embeds the question so it can be compared to stored code/log vectors. |
| **`NexusMemoryManager`** (Phase 5.2) | The thread-safe singleton queried for code + history. |
| **`context_engine`** (Phase 3/4.5) | Supplies the live active-file context from SQLite. |
| **`asyncio.gather`** | Runs the three fetches concurrently so total latency ≈ the slowest single fetch, not the sum. |

---

## 4. Non-blocking triple-fetch — protecting TTFA

The question is embedded once (in an executor thread so the event loop stays
free), then the three fetches run together under `asyncio.gather`:

```python
qvec = await loop.run_in_executor(None, embed_query, question)
active, code_hits, activity_hits = await asyncio.gather(
    fetch_active(),     # SQLite — live file
    fetch_code(),       # ChromaDB codebase_index
    fetch_activity(),   # ChromaDB activity_memory
)
```

Each fetch is wrapped so a failure (empty ChromaDB, locked SQLite) returns an
empty result instead of raising — the turn always proceeds. Because retrieval is
milliseconds and parallel, it doesn't delay Time-to-First-Audio: Piper still
starts speaking the moment the LLM emits its first sentence.

---

## 5. The distance threshold — chatbot ↔ code agent, no router LLM

This is the core idea. Every stored chunk is a vector; the question is a vector;
ChromaDB returns the nearest matches with a **cosine distance** (0 = identical
meaning, larger = less related). We keep only hits within
`RAG_MAX_DISTANCE = 0.55`:

```python
kept = [h for h in hits if h["distance"] <= max_distance]
```

- Ask **"how is login authenticated?"** → the question vector lands very close
  to the `auth.py` chunk vector (distance ~0.34 < 0.55) → it's **kept**, and
  the prompt's `[GLOBAL CODEBASE CONTEXT]` is filled with real code.
- Ask **"how are you today?"** → that vector is far from *every* code/log vector
  (all distances > 0.55) → **everything is dropped**, and the block reads
  "(none relevant to this question)".

So the *geometry of the embedding space itself* decides whether context is
attached — no second LLM classifying intent, no extra inference, no latency.
Combined with the system-role rules ("if casual, ignore the code"), the model
receives an empty context block for chit-chat and behaves like a friend; for
code questions it receives precise, on-topic chunks and behaves like an expert.
The threshold is the dial: lower = stricter (less context, more casual), higher
= looser (more context attached).

---

## 6. Why no separate intent-classifier model is needed

A naive design adds an LLM call to label each question "code" vs "casual" before
answering — doubling latency on CPU-only hardware. We get the same routing for
free because **relevance is already encoded as distance**. Retrieving and
thresholding *is* the intent decision: if nothing in the codebase/history is
near the question, the question isn't about them. One embedding call (which we
need anyway to query) replaces a whole classification model.

---

## 6b. Voice + text both answer on screen & files (update)

- **Querying Nexus AI's own project.** The self-exclusion guard is now a switch:
  `config.EXCLUDE_SELF_CONTEXT = False` (default) lets you ask about Nexus AI's
  OWN files (e.g. "what classes are in test_tracker.py?"). Set it `True` only if
  you run Nexus as a tool while working on *other* projects and want it to
  ignore its own source.
- **Auto-embed the open file.** Each turn first calls
  `VectorIndexer.index_active_file()` (deduped) so the file you're viewing is in
  the vault — but its raw content is also injected live via `[ACTIVE SCREEN
  CONTEXT]`, so "classes in this file" works immediately.
- **Text chat = voice pipeline.** The desktop chat box now sends typed questions
  over the WebSocket (`{"cmd":"ask","text":…}`) to the orchestrator, which runs
  the SAME hybrid retrieval, streams tokens back (`{"type":"token"}`) for live
  display, and speaks them too. So voice and text give identical, screen-aware
  answers. (If the backend is offline, the chat falls back to direct Ollama.)

Verified: live pipeline listed the classes in `test_tracker.py`; the text-turn
emitted user→tokens→answer over the bridge and remembered the exchange.

## 6c. "Current open file" accuracy fix (update)

A subtle bug: if the open file *contains* strings like other filenames or
sample code (e.g. `test_tracker.py` has fixtures mentioning `views.py` and
`def open_one`), the retriever could surface a chunk of it and the small model
would quote the fixture as if it were the file's real content — answering about
the wrong file. Fixes:

- **`[ACTIVE SCREEN CONTEXT]` is authoritative** for "the current/open file":
  the system prompt states its name + contents are ground truth and must be the
  ONLY source for questions about the open file.
- **Global retrieval relabelled `[OTHER PROJECT FILES]`** and the **active
  file's own chunks are excluded** from it — so a retrieved snippet of the open
  file can never masquerade as "the current file".
- **Anti-robot hardened**: "I don't have real-time access" is now explicitly
  forbidden; Nexus is told it CAN see the screen.

Verified: with `test_tracker.py` open, Nexus correctly names it and quotes its
real content (no more `views.py` / `open_one` leakage, no "As an AI").

## 7. Files in this phase

```
tracker/
├── retriever.py            # NEW — embed, parallel triple-fetch, distance filter, master prompt
├── session_orchestrator.py # UPDATED — THINKING uses _gather_context(); self.vault = get_memory()
└── config.py               # UPDATED — RAG_TOP_K, RAG_MAX_DISTANCE
tests/test_tracker.py        # UPDATED — retriever filter/prompt/parallel tests (68 total)
```

---

## 8. Error handling

- **Empty ChromaDB** → `query_*` returns `[]` → empty context block, no crash.
- **Locked / missing SQLite** → active fetch returns an empty `OmniContext`.
- **Embed failure** (Ollama down) → vector queries are skipped (`qvec=None`),
  the turn still runs on the active-file + conversation context.
- `_gather_context` wraps everything; worst case it returns just `[USER SPOKE]: …`.

---

## 9. What's next

With whole-project + full-history retrieval in place, the natural follow-ups are
re-ranking (cross-encoder) for sharper top-k, and auto-indexing on save (wire
the Phase 3.2 watchdog to `VectorIndexer.index_single_file` → `NexusMemoryManager`).
