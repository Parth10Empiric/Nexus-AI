# Phase 7.4 — Streaming-TTS Sentence Chunking + RLHF Prompt Fix

Optimizes the **server-side** (client-server / SaaS) LLM→TTS pipeline. The local
`run_nexus.py` orchestrator path already streamed sentence-by-sentence (via
`tracker/tts_engine.py`'s `SentenceBuffer`) and already had the anti-disclaimer
system prompt (`tracker/context_engine.NEXUS_SYSTEM_PROMPT`). This phase brings
the same two wins to the remote `server.py` + `brain_service.py` path.

## Problem
1. **Full-response latency.** `server._handle_ask` collected the *entire* Ollama
   reply, then called `synthesize_tts(answer)` once. First audio only played
   after the whole answer finished generating.
2. **RLHF disclaimers.** `brain_service.SYSTEM_PROMPT` lacked a strict directive,
   so the model emitted "As an AI language model, I don't have real-time
   access…" even though the RAG pipeline feeds it `[ACTIVE SCREEN]` context.

## Changes

### `brain_service.py`
- **`SYSTEM_PROMPT` override** — explicitly forbids "As an AI language model" /
  "I don't have real-time access" style phrases, and tells the model it DOES
  have real-time access to the screen/files via the `[ACTIVE SCREEN]` context and
  must treat that context as its own direct vision.
- **`SentenceChunker`** — Sentence Boundary Detection. `feed(token)` accumulates
  tokens in a buffer and returns complete speakable chunks the moment a boundary
  is hit: `.!?\n` always flush; `,;:` flush only once the chunk reaches
  `NEXUS_TTS_MIN_CHUNK` chars (default 24) so the synth isn't handed micro-
  fragments. `flush()` returns the trailing partial sentence at end-of-stream.
- **`stream_sentences(messages, num_predict)`** — async generator wrapping
  `stream_chat`; yields whole sentence chunks instead of raw tokens (and passes
  `("__error__", msg)` tuples through unchanged). Provided as the reusable/demo
  form of the technique.

### `server.py` — `_handle_ask`
- Replaces the post-hoc `synthesize_tts(answer)` with a **concurrent pipeline**:
  a background `_tts_pump` task pulls finished sentence chunks off an
  `asyncio.Queue`, synthesizes each (`synthesize_tts` offloads Piper to a worker
  thread), and streams `tts_audio` to the client **in order**. The token loop
  feeds the chunker via `put_nowait`, so generating sentence N+1 never blocks on
  voicing sentence N. UI token streaming (`{"type":"token"}`) is unchanged.

### `run_nexus.py`
- Now also boots the FastAPI Brain server (`server:app`) as a concurrent task in
  the same asyncio loop, so `python run_nexus.py` exercises the server-side path
  too. Toggle with `NEXUS_RUN_BRAIN_SERVER=0`; configure with
  `NEXUS_BRAIN_HOST` / `NEXUS_BRAIN_PORT` (default `0.0.0.0:8000`). Cancelled
  cleanly on shutdown.

## Env knobs
| Var | Default | Meaning |
|-----|---------|---------|
| `NEXUS_TTS_MIN_CHUNK` | `24` | Min chars before a comma/`;`/`:` flushes a chunk |
| `NEXUS_RUN_BRAIN_SERVER` | `1` | Boot the Brain server from `run_nexus.py` |
| `NEXUS_BRAIN_HOST` / `NEXUS_BRAIN_PORT` | `0.0.0.0` / `8000` | Brain server bind |

## Result
First-audio latency drops from "full reply time" to "first sentence time"; remote
voice replies stop opening with RLHF disclaimers.
