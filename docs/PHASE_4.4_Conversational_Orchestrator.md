# Nexus AI — Phase 4.4: The Conversational Orchestrator ("Nexus Ten")

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** the state machine and barge-in interruption work.

---

## 1. What is this phase, in one sentence?

We wired the five separate pieces (capture, transcribe, context, LLM, speak)
into one seamless, interruptible voice assistant — "Nexus Ten" — that you talk
to with a hotkey, that answers about the exact code on your screen, and that
you can cut off mid-sentence to ask something new.

---

## 2. The voice loop (event-driven state machine)

```
            hold Ctrl+Space
   IDLE ───────────────────▶ LISTENING ──(release)──▶ THINKING ──▶ SPEAKING ──▶ IDLE
    ▲                            ▲                    transcribe    stream Ollama
    │                            │                    + mix code    → Pi per speaks
    └──────────── press again (BARGE-IN) ─────────────┘  cancel turn, silence audio
```

This is the real verified path: a spoken *"what does the login function
return?"* → Whisper → inject the on-screen `auth.py` → Ollama → spoken answer
*"The login function returns True only for the admin account."*

---

## 3. How the parts connect

| Phase | Module | Role in the loop |
|---|---|---|
| 4.1 | `audio_capture.py` | Push-to-talk records the question; a press hook fires barge-in. |
| 4.2 | `stt_engine.py` | `transcribe_audio(wav)` → text (run in an executor). |
| 3.3 | `context_mixer.py` | `load_active_context()` + self-exclusion → live screen code. |
| 4 | Ollama `/api/chat` | Streams the answer (persona + code + history). |
| 4.3 | `tts_engine.py` | `feed(token)` speaks each sentence; `interrupt()` silences it. |
| 4.4 | `orchestrator.py` | The asyncio state machine that conducts all of the above. |

---

## 4. The four design pillars

### a) Persona injection
Every turn prepends a strict system instruction: *"Your name is Nexus Ten… speak
naturally… do not use markdown, emojis, or robotic formatting"* — so spoken
answers sound human and the TTS never has to pronounce `**` or backticks.

### b) Screen-aware context
`_build_system_prompt()` pulls the current file from `active_file_context` (via
the Phase 3.3 mixer), applies the **Nexus AI self-exclusion guard**, and embeds
the code so "explain this function" just works. If the active file is Nexus AI's
own source, it falls back to the persona-only prompt (no recursion).

### c) Short-term memory
A `deque(maxlen=5)` holds the last five (user, assistant) turns. `_build_messages`
replays them as `/api/chat` messages, so Nexus Ten remembers what was just
discussed. The cap keeps the prompt small and the CPU fast.

### d) Async, non-blocking
The asyncio loop only ever reads events. The heavy, blocking work — Whisper
transcription and the Ollama HTTP stream — runs in **executor threads**
(`run_in_executor`), so the loop stays free to handle a barge-in at any instant.

---

## 5. Barge-in: stopping safely without orphans or leaks

This is the hardest part. When you press the hotkey mid-answer, three things
must stop **instantly** and **cleanly**:

1. **The audio.** `tts.interrupt()` bumps the TTS generation counter and drains
   its queues; the playback thread checks the counter between ~90ms blocks and
   stops within one block. No thread is killed — it just discards stale audio.
2. **The LLM stream.** The Ollama request runs in an executor thread holding a
   per-turn `threading.Event` (`abort`). Barge-in `set()s` it; the streaming
   loop checks it on every line, breaks, and the `with urllib…` block closes the
   HTTP connection — no lingering socket or subprocess.
3. **The asyncio turn task.** `_barge_in()` calls `task.cancel()` and `await`s it,
   so the coroutine unwinds its `finally`/state cleanly before we move on.

**Why there are no orphans or leaks:**
- We never spawn OS subprocesses for audio (sounddevice writes PCM in-process),
  so there's nothing to leave dangling — interruption is pure in-process state.
- **Per-turn abort events** (not one shared flag) eliminate the classic race: the
  old executor thread keeps its own event reference, so when the next turn
  creates a fresh event the old thread still sees *its* event as set and exits.
- The worker threads (synth, playback, listener) are **long-lived** and never
  cancelled — only their *queued work* is discarded. Bounded queues + the deque
  cap mean memory can't grow across interruptions.
- `await task` after `cancel()` guarantees the cancelled coroutine has fully
  released its resources before a new turn starts.

Verified: a barge-in mid-"speech" cancelled the active task, called
`tts.interrupt()`, and returned to LISTENING with queues empty.

---

## 6. Files in this phase

```
tracker/
├── orchestrator.py      # NEW — asyncio state machine, history, barge-in
├── audio_capture.py     # UPDATED — on_record_start hook (fires barge-in)
├── tts_engine.py        # UPDATED — is_speaking property + _active flag
└── config.py            # UPDATED — OLLAMA_URL/MODEL, CONV_HISTORY_TURNS
tests/test_tracker.py     # UPDATED — 5 orchestrator tests (37 total, all pass)
```

---

## 7. How to run the full assistant

```bash
ollama serve &                         # qwen2.5-coder:1.5b available
./run_tracker.sh &                     # keeps active_file_context fresh (3.1/3.2)
python -m tracker.orchestrator         # Nexus Ten

# Hold Ctrl+Space, ask "what does this function do?", release, listen.
# Press Ctrl+Space again while it's talking to interrupt and ask something new.
```

---

## 8. Project aim — fulfilled

This closes the loop the whole project was built for: a **screen-tracking,
context-aware, voice-driven** assistant. It silently watches what file you're
in (Phases 1–3), and when you ask — by voice — it answers about *that exact
code*, speaks the answer aloud, remembers the conversation, and yields the
moment you want to interrupt. Verified end-to-end on CPU-only hardware.
