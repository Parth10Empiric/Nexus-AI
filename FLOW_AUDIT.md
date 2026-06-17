# FLOW_AUDIT.md — Two-Stage Conversational Pipeline Audit

**Scope:** `run_nexus.py`, `brain_service.py`, `server.py`
**Date:** 2026-06-17
**Auditor:** Systems Architecture review against the Target Workflow Specification.

> **Architectural note first.** `run_nexus.py` is the **process boot harness**, not
> the request dispatcher. It launches three things in one event loop: the Tracker
> daemon (thread), the FastAPI **Brain server** (`server:app` via uvicorn, as a
> concurrent task), and the local Session Orchestrator. The actual **WebSocket
> dispatcher** lives in `server.py` (`_handle_ask` + `websocket_endpoint`), and the
> **3-stage conversational logic** lives in `brain_service.generate_reply`. This
> audit therefore traces the flow through all three files.

---

## 1. Executive Audit Summary

| # | Stage (spec) | Status | Where | Note |
|---|--------------|--------|-------|------|
| 1 | **Triage** — fast classify CASUAL vs CONTEXT | ✅ **PASS (enhanced)** | `brain_service.triage_intent` | Adds a zero-latency deterministic fast-path *before* the Ollama check — exceeds spec. |
| 2 | **Branch A (CASUAL)** — bypass all DBs | ⚠️ **PARTIAL** | `brain_service.generate_reply` `else:` (L591–593) | DB bypass ✅, but it reuses the **full** `SYSTEM_PROMPT`, not a "stripped-down tiny" one. |
| 3 | **Branch B — instant non-blocking filler** | ✅ **PASS** | `generate_reply` L567–572 | Filler yielded **before** awaiting RAG; TTS pump synthesizes it concurrently. |
| 4 | **Branch B — concurrent background RAG** | ✅ **PASS** | `generate_reply` L567–575 | `asyncio.create_task(...)` fires RAG, filler plays, then `await rag_task`. |
| 5 | **Branch B — blend context when file found** | ✅ **PASS** | `retrieve_user_context` + L595 | Active-file contents + RAG hits blended into the prompt. |
| 6 | **Branch B — strict fallback when no data** | ⚠️ **PARTIAL** | `generate_reply` L584–590 | Logic ✅ (decided in code, never hallucinates) but the **exact string differs** from spec. |
| 7 | **Non-blocking event loop (zero lag)** | ✅ **PASS** | `server._handle_ask`, `stream_chat`, `_tts_pump` | All blocking I/O (HTTP, Piper, embed, Chroma) is off-loaded to executors/threads. |
| 8 | **`run_nexus.py` boot is non-blocking** | ✅ **PASS** | `run_nexus._main` | Brain server runs as a task alongside the orchestrator; cancelled cleanly on shutdown. |

**Overall: PASS with 2 partials.** The low-latency skeleton (triage → filler →
concurrent RAG → chunked streaming → code-level fallback) is correctly built and
non-blocking. Two cosmetic/spec-wording gaps remain (see §4).

---

## 2. Target Flow vs. Actual Reality

### 2.1 Desired data flow (per spec)

```
                         ┌──────────────────────────┐
   user question ───────▶│  STAGE 1: TRIAGE          │
   (WebSocket)           │  fast Ollama classify     │
                         └─────────────┬─────────────┘
                            CASUAL  ◀──┴──▶  CONTEXT
                              │                  │
              ┌───────────────▼──┐   ┌───────────▼─────────────────────┐
              │ BRANCH A         │   │ BRANCH B                         │
              │ • skip ALL DBs   │   │ 1. emit verbal FILLER now ───────┼──▶ TTS (instant audio)
              │ • tiny prompt    │   │ 2. background RAG (screen+Chroma)│
              │ • stream answer  │   │ 3a. data found → blend → answer  │
              └────────┬─────────┘   │ 3b. NO data    → "Please open …" │
                       │             └───────────┬──────────────────────┘
                       └──────────────┬──────────┘
                                      ▼
                       SENTENCE-CHUNK every answer → TTS, sentence-by-sentence
```

### 2.2 Actual implementation (as built)

```
server.websocket_endpoint           (auth → registry → receive loop)
   └─ cmd "ask" / audio_end ─▶ server._handle_ask(...)
         └─ async for (kind,payload) in brain_service.generate_reply(...)
              │
              ├─ STAGE 1  triage_intent(question)            ✅ heuristic → LLM(temp0)
              │
              ├─ if CONTEXT:                                  ── BRANCH B
              │     rag_task = create_task(retrieve_user_context)   ✅ concurrent
              │     yield ("filler", …)  ──────────────▶ tts_queue  ✅ instant, non-blocking
              │     user_content = await rag_task                   ✅ overlaps filler
              │     if no ACTIVE_FILE marker AND no RAG marker:
              │         yield NOT_VISIBLE_REPLY ; return            ⚠️ wording differs
              │
              ├─ else:                                        ── BRANCH A (CASUAL)
              │     user_content = question                         ✅ no DB
              │     (build_messages uses FULL SYSTEM_PROMPT)         ⚠️ not "tiny"
              │
              └─ STAGE 3  stream_chat → SentenceChunker             ✅ chunked TTS
                   yield ("token"/"speak"/"answer")
   server side: _tts_pump task synthesizes "speak"/"filler" chunks IN ORDER  ✅
```

---

## 3. Detailed Findings

### ✅ Stage 1 — Triage (`brain_service.triage_intent`, L502–522)
Routes correctly. **Enhancement beyond spec:** a deterministic keyword fast-path
(`_heuristic_intent`) resolves obvious cases with **zero** Ollama latency; only the
ambiguous middle hits the temperature-0 few-shot LLM call (`_triage_llm_sync`, run
in an executor so it never blocks the loop). Fails safe to CONTEXT.

### ✅ Branch B filler + concurrency (`generate_reply`, L566–578)
Exactly to spec. RAG is launched as a background task **before** the filler is
emitted, so the filler audio and the Postgres/Chroma lookup overlap. The filler is
`put_nowait` onto the TTS queue by `server._handle_ask`, and the dedicated
`_tts_pump` task synthesizes it on a worker thread — the generation loop never
blocks on Piper.

### ✅ Stage 3 streaming chunker (`generate_reply`, L598–611; `SentenceChunker`)
Both branches feed one `SentenceChunker`; sentences flush to TTS the instant a
boundary appears. Non-blocking and ordered.

### ✅ Non-blocking guarantees
- `stream_chat` runs the blocking Ollama HTTP read in `run_in_executor`, piping
  tokens back via `asyncio.Queue`.
- `retrieve_user_context` runs embed + Chroma query in an executor.
- `synthesize_tts` runs Piper in an executor; `_tts_pump` keeps audio ordered.
- **No `time.sleep`, no synchronous `urlopen` on the loop thread** anywhere in the
  hot path.

### ✅ `run_nexus.py` boot — does it block the dispatcher? (NO)
This file is the **process boot harness**, not the request dispatcher, so the
"two-stage flow" cannot live here — but it must not *block* it either. Audited
line-by-line:

- **L65–68 — Tracker on a daemon thread.** `threading.Thread(target=tracker.run,
  daemon=True).start()` puts the blocking window-tracking/watchdog loop on its
  own thread; it never sits on the asyncio loop.
- **L71–73 — Brain server as a concurrent task.** `asyncio.create_task(_serve_brain())`
  launches `server:app` (the real WebSocket dispatcher) *alongside* the
  orchestrator in the same loop — not via a blocking `uvicorn.run()`. This is the
  key non-blocking decision: both servers cooperate on one event loop.
- **L49–61 — `_serve_brain()`** awaits `uvicorn.Server.serve()` (no `reload=`, which
  would fork) and, on `CancelledError`, sets `server.should_exit = True` then
  re-raises so uvicorn unwinds its own tasks — clean, non-blocking shutdown.
- **L77–88 — orchestrator + teardown.** `await orch.run()` is the long-lived call;
  the `finally` signals the tracker to stop (`_running = False`, `t.join(timeout=3)`)
  and `brain_task.cancel()` + `await brain_task`, swallowing the cancellation.

**Verdict:** no `time.sleep`, no synchronous server start, no `urlopen` on the loop
thread. `run_nexus.py` boots all three subsystems concurrently and tears them down
cleanly — it neither implements nor obstructs the 3-stage flow.

### ⚠️ FINDING #1 — Branch A is not a "stripped-down tiny" prompt
**Reality:** the CASUAL branch sets `user_content = question` (DB bypass is correct)
but then calls `build_messages(...)`, which always prepends the **full**
`SYSTEM_PROMPT` (screen-awareness rules, grounding rules, style rules — ~1.5 KB).
**Impact:** a casual "hi" still pays prompt-eval cost for screen rules it will never
use — small extra latency and occasional off-topic screen mentions.
**Severity:** Low (latency/quality), not a correctness break.

### ⚠️ FINDING #2 — Fallback string does not match the spec verbatim
**Reality:** `NOT_VISIBLE_REPLY = "Please open that file on your screen so I can see its data."`
**Spec:** `"Please open that file on your screen and ask me any question related to this file."`
The *logic* is correct and robust (decided in code at L584, never reaches the LLM,
so it cannot hallucinate), but the wording differs.
**Severity:** Low (cosmetic).

### ℹ️ Non-issue checks
- **"RAG loaded too early"?** No. RAG only fires inside the CONTEXT branch, after
  triage. CASUAL never touches a DB.
- **"Execution loop blocks"?** No blocking call found on the loop thread.
- **"Fallback missing"?** Present and stronger than spec (code-level, not prompt).

---

## 4. The Fix — Corrected Dispatcher / Pipeline Snippet

> The following aligns Branch A to a *tiny* prompt and the fallback to the *exact*
> spec wording. **This is the proposed change only — no source files were modified
> by this audit.** Apply in `brain_service.py`.

```python
# ── add near the other prompt constants ──────────────────────────────────────
# Stripped-down CASUAL persona: no screen/grounding rules, just talk. Smaller
# prompt = faster prompt-eval for "hi" / "what is an API?" style turns.
CASUAL_SYSTEM_PROMPT = (
    "You are Nexus, a warm, witty human-like assistant. Reply in 1-2 short, "
    "natural spoken sentences. No code blocks, no markdown, no lists — just talk. "
    "Never say you are an AI or a language model."
)

# Exact spec wording for the deterministic fallback.
NOT_VISIBLE_REPLY = (
    "Please open that file on your screen and ask me any question related to this file."
)


# ── corrected generate_reply (only the branch + build differ) ────────────────
async def generate_reply(username, question, history, *, active=None, voice=False):
    num_predict = OLLAMA_NUM_PREDICT_VOICE if voice else OLLAMA_NUM_PREDICT_TEXT

    # STAGE 1 — triage (heuristic fast-path → temp-0 LLM fallback)
    needs_context = await triage_intent(question)

    if needs_context:
        # BRANCH B — concurrent RAG + instant filler
        rag_task = asyncio.create_task(
            retrieve_user_context(username, question, active=active)
        )
        for chunk in _chunk_static(_pick_filler()):
            yield ("filler", chunk)                     # instant audio, non-blocking
        try:
            user_content = await rag_task               # overlaps the filler
        except Exception as exc:
            log.warning("RAG failed: %s", exc)
            user_content = question

        # Strict fallback — decided in CODE so a weak model can't hallucinate.
        if _ACTIVE_FILE_MARKER not in user_content and _RAG_MARKER not in user_content:
            for chunk in _chunk_static(NOT_VISIBLE_REPLY):
                yield ("speak", chunk)
            yield ("token", NOT_VISIBLE_REPLY)
            yield ("answer", NOT_VISIBLE_REPLY)
            return

        messages = build_messages(list(history), user_content, voice=voice)  # full prompt
    else:
        # BRANCH A — CASUAL: bypass every DB AND use the tiny prompt.
        system = CASUAL_SYSTEM_PROMPT + (VOICE_CLAUSE if voice else "")
        messages = (
            [{"role": "system", "content": system}]
            + list(history)
            + [{"role": "user", "content": question}]
        )

    # STAGE 3 — stream + sentence-chunk for both branches
    chunker = SentenceChunker()
    parts = []
    async for token in stream_chat(messages, num_predict=num_predict):
        if isinstance(token, tuple):                    # ("__error__", msg)
            yield ("error", token[1]); return
        parts.append(token)
        yield ("token", token)
        for chunk in chunker.feed(token):
            yield ("speak", chunk)
    tail = chunker.flush()
    if tail:
        yield ("speak", tail)
    yield ("answer", "".join(parts))
```

### WebSocket dispatcher (already correct — shown for completeness)

The dispatcher in `server._handle_ask` requires **no change**; it already routes
the typed events non-blockingly and runs the TTS pump as a background task:

```python
async for kind, payload in brain_service.generate_reply(
        username, text, list(history), active=active, voice=voice):
    if   kind == "error":  ...                          # surface + stop
    elif kind == "filler": send token + tts_queue.put_nowait(payload)
    elif kind == "token":  send {"type":"token"}        # live UI text
    elif kind == "speak":  tts_queue.put_nowait(payload)  # → _tts_pump (ordered)
    elif kind == "answer": answer = payload
# finally: tts_queue.put_nowait(None); await tts_task   # drain last audio
```

---

## 5. Verdict

The pipeline **already executes the low-latency 3-stage flow** with correct
non-blocking concurrency and a robust, code-level "Not Visible" fallback. To be
100% spec-conformant, apply the two changes in §4:

1. Give **Branch A** its own `CASUAL_SYSTEM_PROMPT` (faster, cleaner casual chat).
2. Change `NOT_VISIBLE_REPLY` to the **exact** spec sentence.

Both are low-risk and isolated to `brain_service.py`.
