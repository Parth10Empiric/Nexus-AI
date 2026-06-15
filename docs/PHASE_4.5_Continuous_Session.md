# Nexus AI — Phase 4.5 (Refactor): Continuous Multi-Turn Voice Session

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** the four-state machine, VAD endpointing, and barge-in work.

---

## 1. What is this phase, in one sentence?

We upgraded Nexus from a single-shot voice assistant into a **continuous
conversation**: say the wake word once, then talk back and forth naturally —
Nexus remembers the thread, you can interrupt it mid-sentence, and "bye Nexus"
puts it back to sleep.

---

## 2. The four-state machine

```
        wake word ("hey …")              speech ends (VAD)
  STANDBY ───────────────▶ ACTIVE_LISTENING ───────────────▶ THINKING
   ▲  ▲                       ▲     │  "bye nexus" → clear mem, beep, STANDBY
   │  │                       │     │  silence timeout → STANDBY
   │  │      barge-in         │     ▼
   │  └───── (sustained ──── SPEAKING ◀──── stream Ollama + Piper
   │          voice)           │
   │   natural finish ─────────┘   (NO wake word needed → ACTIVE_LISTENING)
   └─ "bye nexus" / idle timeout
```

One always-open mic serves all four states; the orchestrator just changes the
front-end's *mode*. Verified by tests: multi-turn loop-back, end-phrase reset,
barge-in, and wake-from-standby all transition correctly.

---

## 3. Tools & why

| Tool | Role |
|---|---|
| **`openwakeword`** | STANDBY passive trigger (<2% CPU). |
| **Energy VAD** (numpy RMS) | ACTIVE_LISTENING endpointing + SPEAKING barge-in guard. Dependency-free; `webrtcvad` is a drop-in upgrade. |
| **`asyncio`** | The state machine; heavy work (Whisper/Ollama) runs in executors so the loop never blocks. |
| **`faster-whisper` / `ollama` / `piper-tts`** | Transcribe / think / speak (Phases 4.2–4.3). |
| **WebSocket bridge** | Emits Sleeping/Listening/Thinking/Speaking to the React UI. |

---

## 4. VAD endpointing — knowing when you stopped talking

In ACTIVE_LISTENING the front-end computes each 80ms frame's RMS energy:
- It waits for energy above `VAD_START_RMS` to mark **speech started** (ignoring
  leading silence).
- Once started, it accumulates frames and counts **trailing silence**; after
  ~0.9s of quiet (or a hard max) it decides you're done, packs the frames into
  an in-memory WAV, and hands it to Whisper.
- If no speech begins within `SESSION_IDLE_TIMEOUT_SEC`, it returns `None` and
  the session goes back to STANDBY — so an abandoned session sleeps itself.

Verified: loud-then-silent frames endpoint an utterance; all-silent frames hit
the idle timeout.

---

## 4b. Half-duplex turn-taking + Shift+I interrupt (current model)

Nexus now takes turns like a walkie-talkie, which removes the echo problem (the
mic used to hear Nexus's own voice during SPEAKING):

```
🎧 listen → (mic OFF) → 🧠 think → 🔊 speak → 🎧 listen again → …
```

- While **thinking and speaking the mic is OFF** (`MODE_OFF`) — Nexus never
  hears itself, and there is no false voice barge-in.
- When the reply **finishes**, the mic re-opens automatically (`🎧 Listening...`)
  with **no wake word** between turns.
- **Interrupt = Shift+I** (a global pynput hotkey) or the dashboard's
  **Interrupt** button (`{"cmd":"interrupt"}`). Either cancels the current reply
  and returns to listening. Voice barge-in is intentionally disabled.
- **Stop the agent**: say any goodbye — **"bye"**, "bye jarvis", "goodbye",
  "ok bye", "see you", "sleep nexus" — or toggle Voice Agent Mode off. A
  goodbye **fully turns the agent off**: it does NOT reply, stops the wake
  listener, clears memory, plays a chime, and flips the dashboard toggle OFF.
  Once off, wake/utterance events are ignored (guarded by `_armed`). To use it
  again you **toggle on and say the wake word again** ("hey jarvis"). Detection
  requires the goodbye to be the whole utterance (or its start), so "how do I
  say goodbye in code?" is NOT an exit.
- **Easy to change:** the wake word and stop words live in one labelled block
  in `config.py` — `WAKE_WORD_MODEL`, `STOP_WORDS`, `STOP_PHRASES` (look for the
  "🎙️ VOICE CONTROL" banner).

Verified: "bye" turns the agent off with NO reply (mic stopped, memory cleared,
UI off); a wake event after off is ignored; re-activating returns to STANDBY so
the wake word is required again.

## 5. Barge-in — interrupting safely (the hard part)

During SPEAKING the mic **stays open** in GUARD mode. The guard counts
*consecutive* frames above a **higher** threshold (`BARGEIN_RMS`, set above the
listening threshold to resist the AI's own audio echo). Only when voice is
**sustained** for `BARGEIN_SUSTAIN_MS` (~0.5s) does it fire — a brief blip or a
single echoed syllable is ignored. Verified: a 2-frame blip does nothing; ~0.5s
of voice triggers barge-in.

When barge-in fires, `_cancel_turn()`:
1. `self._abort.set()` — the Ollama executor thread checks this each line, stops
   reading, and the `with urllib…` block closes the HTTP socket.
2. `self.tts.interrupt()` — bumps the TTS generation counter and drains its
   queues; the playback thread stops within ~one 90ms block.
3. `task.cancel()` + `await task` — the turn coroutine unwinds cleanly.

Then it jumps straight to ACTIVE_LISTENING to hear you.

---

## 6. Multi-turn memory & session end

- **No repeated wake word:** when SPEAKING finishes naturally, the orchestrator
  calls `_enter_listening()` directly, which prints `🎧 Listening...` to the
  terminal. You just keep talking — the wake word is only needed once.
- **Stays awake on silence:** with `SESSION_STAY_AWAKE = True`, a silent VAD
  timeout does NOT drop to STANDBY — it silently re-arms the mic and keeps
  listening. The session loops **forever until you say an end phrase**.
- **Rolling memory:** a `deque(maxlen=10)` of messages lives for the whole
  session, formatted into the prompt's `[CONVERSATION THREAD]` block. Cleared on
  session end.
- **Interrupt words vs. questions:** if the WHOLE utterance is a cancel word
  ("stop", "okay", "wait", "cancel", "enough", …) it is NOT answered — Nexus
  just returns to `🎧 Listening...`. Anything longer is treated as a real
  question. This is how "stop" cuts a reply without starting a new answer.
- **Session end ("bye jarvis"):** if a transcript contains "bye jarvis",
  "goodbye jarvis", "sleep jarvis" (or the "nexus" variants), memory is cleared,
  a short two-tone chime plays, and it returns to STANDBY. Verified by test.

### The continuous flow you actually experience
```
say "hey jarvis"          → 🔔 awake → 🎧 Listening...
ask question 1            → 🤖 spoken answer → 🎧 Listening...   (no re-wake!)
ask question 2            → 🤖 spoken answer → 🎧 Listening...
(talk over the reply / say "stop")  → reply stops → 🎧 Listening...
say "bye jarvis"          → 👋 chime → back to sleep (STANDBY)
```

---

## 6b. Situational awareness — curing "context bias"

Small models blindly use whatever is in their context. With the screen code in
every prompt, asking "what's your favorite fruit?" produced robotic answers
that forced in the code ("…but I see you're editing tts_engine.py"). The fix is
prompt structure, not a second model:

- **Persona + conditional rules live in the SYSTEM role** (`NEXUS_SYSTEM_PROMPT`):
  it forbids "As an AI"/"I don't have preferences", demands a confident human
  tone, and — critically — tells the model to FIRST decide *code question vs.
  casual chat* and to **ignore the screen entirely for casual questions**.
- **Screen/history/question go in the USER role** (`build_session_context_block`),
  clearly labelled, so they're available data — not standing instructions.

Verified live: "favorite fruit?" → *"Honestly, I love apples!"* (no code
mention, no robotic disclaimer); "what does the class on my screen do?" →
an accurate description of the on-screen `TTSEngine`.

## 7. The omniscient session prompt

```
You are Nexus, an elite human-like AI developer assistant. You are in an active conversation. …
[LIVE SCREEN]: User is currently viewing {active_file} containing: {file_content}.
[RECENT HISTORY]: {last_5_logs}.
[CONVERSATION THREAD]:
{session_memory}
[USER SPOKE]: {transcribed_audio}
```
Built by `context_engine.build_session_prompt()`. The self-exclusion guard still
applies, so Nexus never ingests its own source.

---

## 8. Files in this phase

```
tracker/
├── voice_frontend.py        # NEW — one mic, 4 modes (standby/listen/guard/off), energy VAD
├── session_orchestrator.py  # NEW — the continuous 4-state machine + barge-in + memory
├── context_engine.py        # UPDATED — build_session_prompt() w/ [CONVERSATION THREAD]
└── config.py                # UPDATED — VAD/barge-in/session settings
frontend/src/components/VoiceAgentPanel.jsx  # UPDATED — "Sleeping" state
tests/test_tracker.py         # UPDATED — VAD, barge-in, session machine (47 total, all pass)
```
The earlier single-turn `orchestrator.py` (PTT) and `wake_orchestrator.py`
remain for reference; `session_orchestrator.py` is the continuous successor.

---

## 9. Performance — making it fast to listen & respond

The agent felt slow because of cold starts and the LLM unloading between turns.
Fixes applied (all in `config.py` / `session_orchestrator.py`):

| Fix | Why it speeds things up |
|---|---|
| **Warm-up at startup** (`_warmup`) | Whisper, Piper, and the LLM are all pre-loaded *before* the first wake word (Whisper + LLM concurrently in executors). The first question is no longer slowed by multi-second model loads. |
| **`keep_alive: "30m"`** on every Ollama call | Keeps the 1.2GB model **resident in RAM** between turns. Without it Ollama unloads after ~5min idle and each turn pays a multi-second reload. Verified: `ollama ps` shows the model pinned; **time-to-first-token ≈ 0.5s**, short reply ≈ 2s. |
| **`num_predict: 220`** cap | Spoken answers stay short — voice isn't an essay — so replies finish fast. |
| **`VAD_SILENCE_MS = 650`** | Snappier end-of-speech detection (was 900ms) so Nexus starts thinking sooner after you stop talking. |
| **Sentence-buffered TTS** (Phase 4.3) | Nexus starts speaking the first sentence while the rest still generates — so perceived latency ≈ first-sentence time, not full-answer time. |
| **Optional `tiny.en`** | Set `STT_MODEL_SIZE = "tiny.en"` for ~2–3× faster transcription on CPU (slightly lower accuracy — fine for short commands). |

End-to-end you typically hear the first words ~0.5–1s after you stop speaking.

---

## 10. How to run

```bash
pip install -r requirements.txt
ollama serve & ./run_tracker.sh &
python -m tracker.session_orchestrator      # auto-warms models + auto-arms
cd frontend && npm run tauri:dev
```

`SESSION_AUTOSTART = True` means the orchestrator **arms itself on launch** — no
UI toggle required. You'll see in the terminal:
```
⏳ Warming up models (Whisper, Piper, LLM)… → ✅ Models ready.
💤 Standby — say the wake word ('hey jarvis') to start.
🔔 Wake word detected — Nexus is awake!
🎧 Listening...   ← every time it's your turn
```
Say **"hey jarvis"** once, then ask as many questions as you like — it returns
to `🎧 Listening...` after every answer with **no re-wake**. Talk over it or say
**"stop"** to interrupt; say **"bye jarvis"** to sleep. The dashboard toggle
mirrors the state and can disarm/re-arm on top of autostart.

---

## 11. Honest caveats

- **Acoustic echo:** with speakers + an open mic, the AI's own voice can leak
  into the mic. We mitigate with a higher, sustained barge-in threshold, but
  true robustness needs acoustic echo cancellation (or headphones).
- Verified all logic (VAD, barge-in, state transitions, prompts) with synthetic
  audio + mocked LLM/TTS; the live mic↔speaker path is the same one proven in
  Phases 4.1–4.4.
