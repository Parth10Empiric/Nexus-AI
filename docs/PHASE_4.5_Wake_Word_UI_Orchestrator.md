# Nexus AI — Phase 4.5: UI Orchestrator & Wake-Word Context Engine

> A learning guide. Plain-language explanation of **what** we built, **why**,
> and **how** wake-word activation, omniscient context, and UI state sync work.

---

## 1. What is this phase, in one sentence?

We turned Nexus Ten into an always-watching, hands-free assistant: a "Voice
Agent Mode" toggle in the dashboard arms a low-CPU wake-word listener, and when
you say the wake phrase it pulls *everything* it knows about your screen (live
code + recent history) and answers as a human-like "Nexus Ten" — while the UI
shows its live state.

---

## 2. The flow

```
  React toggle ON ──ws──▶ Python: arm wake listener (openwakeword, <2% CPU)
                                   │  "hey jarvis" detected
        emit "listening" ◀─ws──────┤  capture utterance (energy endpointing)
                                   ▼
  transcribe (Whisper) ─▶ assemble OMNISCIENT context ─▶ emit "thinking"
        (active file + code + last 5 window/file changes from SQLite)
                                   ▼
  master "Nexus Ten" prompt ─▶ Ollama /api/chat ─▶ Piper speaks ─▶ emit "speaking"
                                   ▼
                              back to "standby"
```

Verified: asked "what were the last files I worked on?" → *"The last files you
worked on were serializers.py and auth.py from the EMPIRA_HR project."*; asked
"what am I looking at?" → an accurate description of the on-screen serializer.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`openwakeword`** | Tiny ONNX wake-word model. Runs on every 80ms mic frame at **<2% CPU**, so the mic can stay open all day without waking Whisper. |
| **WebSocket (`websockets`)** | The Python orchestrator is a *separate process* from the Tauri/React app, so a local socket is the clean cross-process channel for the toggle command and live state events. |
| **`sounddevice`** | One 16kHz input stream feeds the wake detector, then captures the question. |
| **read-only SQLite** | The context engine reads `active_file_context` + `activity_log` without touching the tracker's writer. |

---

## 4. Wake word: why it beats running Whisper continuously

Whisper — even `base.en` int8 — is a transformer. Running it nonstop on the
i5-6500 would peg a core (tens of % CPU) and heat the machine, just to throw
away mostly-silence. `openwakeword` is a *much* smaller model designed for
exactly this "is the magic phrase present?" yes/no question:

- It processes one **80ms frame (1280 samples)** at a time through a tiny
  mel-spectrogram + a small classifier — orders of magnitude less compute than
  a full speech-recognition decode.
- It stays in a passive loop at **<2% CPU**. Only when a frame scores above the
  threshold does it hand off to the heavy `faster-whisper` transcription — which
  then runs for the ~1s of a single question, not 24/7.
- This is the standard two-tier "small gate in front of a big model" pattern:
  cheap always-on detector → expensive on-demand worker. It both saves CPU and
  prevents random background speech/noise from triggering full transcription.

> Note: openwakeword ships pretrained `hey_jarvis`, `hey_mycroft`, `alexa`, etc.
> There is no pretrained "Hey Nexus" — a custom phrase needs a trained model
> (openwakeword provides tooling). We default to `hey_jarvis`; the assistant
> still **replies as "Nexus Ten"** regardless of the trigger word.

### Capturing the question (energy endpointing)
After waking, the same stream switches to CAPTURING mode: it accumulates frames
and watches the RMS energy. Once it sees ~0.9s of continuous silence (or hits
the max duration), it knows you finished speaking, packs the frames into an
in-memory WAV, and hands it to Whisper — no fixed-length guesswork.

## 5. Omniscient context: how "recent history" gives native memory of past files

When you ask, `context_engine.assemble_context()` instantly gathers three things
from `local_logs.db`:

1. **Active window title + file name** — what you're on right now.
2. **Raw code of the active file** — from the Phase 3 `active_file_context`
   table (self-excluded if it's Nexus AI's own source, to avoid recursion).
3. **Last 5 distinct window/file changes** — from `activity_log`, deduped and
   ordered oldest→newest with timestamps.

These are fused into the master prompt's `[RECENT HISTORY]` block. Because the
list of files you visited is **literally in the prompt text**, the model can
answer "what did I work on?" *natively* — it's not recalling from training or
guessing; it's reading your actual recent timeline and summarizing it. That's
why a question with no code at all ("list my recent files") still gets an
accurate, specific answer. A small closing directive tells the model to treat
the LIVE SCREEN and RECENT HISTORY as its source of truth, which is what makes
the 1.5B model reliably use them instead of asking you for details.

---

## 6. UI state synchronization

The `UIBridge` WebSocket server emits `{"type":"state","state":...}` on every
transition (`off → standby → listening → thinking → speaking → standby`). The
React `useAgentSocket` hook receives them and the `VoiceAgentPanel` renders a
pulsing colored dot + label, so the dashboard always mirrors what Nexus Ten is
doing. The toggle sends `{"cmd":"activate"|"deactivate"}` back. The hook
auto-reconnects if the Python backend restarts. Verified: command-in / state-out
round-trip over a real socket.

---

## 7. Files in this phase

```
tracker/
├── wake_word.py          # NEW — openwakeword listener + utterance capture
├── context_engine.py     # NEW — omniscient context + master prompt + DB fetch
├── ui_bridge.py          # NEW — WebSocket state emitter + command receiver
├── wake_orchestrator.py  # NEW — ties wake+context+Ollama+TTS+UI together
└── config.py             # UPDATED — wake/UI settings
frontend/src/
├── hooks/useAgentSocket.js        # NEW — WS client + auto-reconnect
├── components/VoiceAgentPanel.jsx # NEW — toggle + live state visualizer
└── App.jsx                        # UPDATED — mounts the panel
requirements.txt          # UPDATED — openwakeword, websockets
tests/test_tracker.py     # UPDATED — context-engine + UI-bridge tests (40 total)
```

---

## 8. How to run

```bash
pip install -r requirements.txt
python -c "import openwakeword.utils as u; u.download_models()"   # one-time
ollama serve & ./run_tracker.sh &                                # brain + eyes
python -m tracker.wake_orchestrator                              # wake agent + UI bridge
cd frontend && npm run tauri:dev                                 # the dashboard
# Flip "Voice Agent Mode" ON, say the wake word, then ask about your screen.
```

---

## 9. Project aim — fully realized

Nexus Ten now *continuously watches* your screen, listens hands-free, and —
when called — answers about the exact code in front of you AND what you were
doing earlier, speaking like a human colleague, with the dashboard reflecting
its every state. The whole loop runs locally on CPU-only hardware.
