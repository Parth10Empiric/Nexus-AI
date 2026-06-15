# Nexus AI — Phase 5.2: Local Database Memory Management (Singleton Vault)

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** the singleton + locks keep the vector DB safe under concurrency.

---

## 1. What is this phase, in one sentence?

We wrapped the two ChromaDB collections behind a single, thread-safe
`NexusMemoryManager` — one shared database connection for the whole app, so the
background tracker and the voice orchestrator can read and write the AI's
long-term memory at the same time without ever corrupting it.

---

## 2. The shape

```
        ┌──────────────── one process ─────────────────┐
        │   watchdog daemon        voice orchestrator  │
        │        │                       │             │
        │        └─────────┬─────────────┘             │
        │                  ▼                           │
        │        NexusMemoryManager()  ← SINGLETON     │
        │            (one RLock guards all access)     │
        │          ┌──────────┴──────────┐             │
        │   codebase_index          activity_memory    │
        └──────────── chromadb.PersistentClient ───────┘
                       ./nexus_memory_db (SQLite-backed)
```

There is exactly **one** client and **one** lock, no matter how many threads
call `NexusMemoryManager()`.

---

## 3. The API

| Method | What it does |
|---|---|
| `upsert_code_chunk(file_path, chunk_content, embedding, chunk_index, start_line, end_line)` | Store/replace a code chunk; metadata = absolute path + file name + line range. |
| `delete_file(file_path)` | Remove all chunks for a file (call before re-indexing on save). |
| `query_codebase(query_embedding, n_results=3)` | Top-k code matches with metadata + distance. |
| `upsert_activity_log(timestamp, log_content, embedding, app_name, title)` | Store a log; timestamp normalized to a **Unix int**, metadata = unix_ts + app name + title. |
| `query_activity(query_embedding, n_results=5)` | Top-k history matches. |
| `stats()` | Counts per collection. |

Embeddings are computed elsewhere (Phase 5.1, `nomic-embed-text`) and passed in,
so this layer is pure, fast storage.

---

## 4. Metadata rules

- **Code:** `{path (absolute), file_name, chunk_index, start_line, end_line}`.
  Unknown line numbers store `-1` (Chroma metadata can't hold `None`).
- **Activity:** `{unix_ts (int), app_name, title}`. An ISO-8601 string is parsed
  to a Unix timestamp automatically; a numeric value is used as-is.
- `_clean_meta()` drops `None`s and coerces odd types, since Chroma only accepts
  `str/int/float/bool`.

---

## 5. How Singleton + locks prevent corruption

Picture the danger: the `watchdog` daemon fires `upsert_code_chunk()` the exact
instant the voice orchestrator runs `query_activity()`. ChromaDB persists to
**SQLite**, and SQLite allows only one writer at a time per database file. Two
*independent* clients opened on the same folder, writing concurrently, produce
`database is locked` errors or — worse — interleaved writes.

Two mechanisms remove that risk:

1. **Singleton = one client.** `__new__` uses a class-level lock with
   double-checked init so every `NexusMemoryManager()` call in the process
   returns the *same* instance, holding the *same* `PersistentClient`. There is
   never a second connection competing for the SQLite file. (Verified: three
   constructions return one identity.)
2. **One re-entrant lock = serialized access.** Every `upsert_*`, `query_*`,
   `delete_file`, and `stats` runs inside `with self._lock:`. So even though the
   watchdog thread and the voice thread *call* concurrently, their actual DB
   operations execute **one at a time**. The writer finishes its transaction
   before the reader starts, so SQLite never sees two writers and never locks.
   An `RLock` (re-entrant) means a method that internally calls another locked
   method won't deadlock on itself.

The combination turns "two threads racing on a SQLite file" into "two threads
queuing politely at one door." Verified: 6 threads (3 writers + 3 readers)
hammering the manager produced **zero errors**.

The tiny trade-off is that DB ops are serialized rather than truly parallel —
but vector upserts/queries are millisecond-scale, so the lock is essentially
never contended in practice, and correctness beats a parallelism we don't need.

---

## 6. Files in this phase

```
tracker/
├── memory_manager.py    # NEW — NexusMemoryManager singleton + thread-safe CRUD
└── config.py            # UPDATED — NEXUS_MEMORY_DIR
tests/test_tracker.py     # UPDATED — singleton, metadata, concurrency (64 total)
nexus_memory_db/          # the persistent store (created on first use)
```

---

## 7. How it fits with Phase 5.1

Phase 5.1's `VectorIndexer` does chunking + embedding; Phase 5.2's
`NexusMemoryManager` is the storage authority. The natural wiring is for the
indexer (and the watchdog hook) to embed, then call
`get_memory().upsert_code_chunk(...)`, and for the retriever (Phase 5.3) to call
`get_memory().query_codebase(...)` / `query_activity(...)` — everyone sharing
the one safe connection.

```python
from tracker.memory_manager import get_memory
mem = get_memory()                       # same instance everywhere
mem.upsert_code_chunk(path, chunk, emb, chunk_index=i, start_line=s, end_line=e)
mem.query_codebase(query_emb, n_results=3)
```

---

## 8. What's next (Phase 5.3)

A retriever that, per question, embeds the query and pulls top-k from the right
collection — feeding whole-project code and full-timeline history into the
Nexus prompt, on top of the live active-file context from Phase 4.5.
