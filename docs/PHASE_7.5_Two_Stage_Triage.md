# Phase 7.5 — Two-Stage Fast Triage + Concurrent RAG

Builds on Phase 7.4 (streaming-TTS chunking). Removes the dead-time where every
turn paid full RAG cost (Ollama embed → Chroma query → vault assembly) before the
first token — even for "hi" or "what is an API?".

## Architecture (all in `brain_service.generate_reply`, driven by `server._handle_ask`)

### Stage 1 — Intent triage (`triage_intent`)
Two tiers, cheapest first:
1. **Deterministic fast-path** (`_heuristic_intent`) — zero latency. Greetings →
   CASUAL; deixis + code/work verbs ("fix this", "this error", "my code", "why is
   my…") → CONTEXT; "what is a/an…", "who are you" → CASUAL.
2. **LLM fallback** (`_triage_llm_sync`) — only for the ambiguous middle. A
   few-shot prompt at **temperature 0**, `num_predict=4`, non-streaming. Run in an
   executor so it never blocks the loop.

Fails **safe**: empty/garbled verdict → CONTEXT (skipping context on a code
question is far worse than wasting RAG on a casual one).

### Stage 2 — Branching
- **Branch A (CONTEXT):** `asyncio.create_task(retrieve_user_context(...))` fires
  RAG in the background, then a canned human filler (`FILLER_PHRASE`) is yielded
  immediately as `("filler", …)` chunks so the client's TTS starts speaking at
  once. `await rag_task` collects the heavy context that assembled concurrently.
- **Branch B (CASUAL):** RAG skipped entirely; answer streams straight away.

> Design choice: triage returns a one-word **class** and the **server owns the
> filler wording**. Asking the model to emit a verbatim phrase for the server to
> string-match would be fragile and waste tokens.

### Stage 3 — Sentence chunking (both branches)
The answer feeds a `SentenceChunker`; completed sentences are yielded as
`("speak", chunk)` for the TTS pump while raw tokens go out as `("token", …)` for
live UI text. The TTS pump (in `server.py`) synthesizes chunks in order on a
background task — generation, RAG, and speech all overlap.

## Event protocol — `generate_reply` yields
| Event | Routed by server to |
|-------|---------------------|
| `("filler", str)` | `{"type":"token"}` (UI) + TTS pump |
| `("token", str)`  | `{"type":"token"}` (UI live text) |
| `("speak", str)`  | TTS pump → `{"type":"tts_audio"}` |
| `("answer", str)` | `{"type":"answer"}` + stored in history |
| `("error", str)`  | `{"type":"answer","text":"⚠️ LLM error: …"}` |

The filler is spoken/shown but **not** stored in conversation history.

## Real-time screen awareness (anti-hallucination)
`retrieve_user_context` always emits an `[ACTIVE SCREEN CONTEXT]` block labelling
the latest capture as `CURRENTLY ACTIVE WINDOW` / `CURRENTLY ACTIVE FILE` (with
"Its exact contents are:" when a file is genuinely open), and demotes RAG hits to
`[BACKGROUND REFERENCE — … NOT … on screen]`. The system prompt forbids deciding
"what's on screen" from chat history.

**"Not Visible" fallback is decided in CODE, not by the model.** A 1.5B model
can't reliably branch on prose, so `generate_reply` checks for the active-file /
RAG markers: if a CONTEXT turn produced neither, it returns `NOT_VISIBLE_REPLY`
("Please open that file on your screen so I can see its data.") without ever
calling the LLM — eliminating both the "ignores the open file / hallucinates an
old one" and the "guesses a file that isn't open" failures.

## Env knobs
| Var | Default | Meaning |
|-----|---------|---------|
| `NEXUS_FILLER_PHRASE` | _(unset → rotate `FILLER_PHRASES` pool)_ | Force ONE fixed Branch-A filler |
| `NEXUS_TRIAGE_NUM_PREDICT` | `4` | Triage output cap |

## Verified
Live triage (qwen2.5-coder:1.5b) classifies correctly: greetings/"what is an
API?"/"tell me a joke" → CASUAL; "fix this function"/"explain this error"/"why is
my code failing?"/"what file is open?" → CONTEXT.
