# Nexus AI — Phase 4.3: Local Text-to-Speech Output (Piper TTS)

> A learning guide. Plain-language explanation of **what** we built, **why**
> each tool, and **how** the streaming sentence-buffer and interruption work.

---

## 1. What is this phase, in one sentence?

We made the assistant **talk back**: as Ollama streams its answer token-by-
token, we group tokens into sentences, strip Markdown, summarize code blocks,
and speak each sentence aloud with Piper — all locally, in real time, and
instantly interruptible.

---

## 2. The pipeline (three decoupled stages)

```
  Ollama tokens ──▶ feed(token)
                       │
                       ▼  SentenceBuffer  (parse + clean markdown + code summary)
                  text queue  ──────────────┐
                                            ▼  SYNTH thread: Piper(sentence) -> PCM
                                       audio queue ─────┐
                                                        ▼  PLAY thread: sounddevice
                                                     🔊 speakers
```

While sentence A is playing, the synth thread is already turning sentence B
into audio — so there's no gap between sentences after the first one.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`piper-tts`** | Fast, local **neural** TTS using ONNX voice models — natural-sounding, runs fine on CPU, no cloud. |
| **`en_US-lessac-medium.onnx`** | A pre-downloaded Piper voice (clear US English, medium quality/speed balance). |
| **`onnxruntime`** | Runs the ONNX voice model efficiently on the CPU. |
| **`sounddevice`** | Plays the PCM audio through an `OutputStream` we can write in small blocks (for fast interruption). |
| **`queue.Queue` (stdlib)** | Two thread-safe FIFOs connect the three stages. |

---

## 4. Time-to-first-audio: how the sentence buffer minimizes delay

The naive approach — wait for the whole answer, then speak — feels sluggish: on
a CPU the model may take many seconds to finish a paragraph, and the user hears
nothing the entire time. Our `SentenceBuffer` fixes this:

- It **accumulates tokens** as they arrive and watches for a sentence boundary
  (`.`, `?`, `!`, or `\n`).
- The **instant** the first boundary appears, that sentence is cleaned and
  pushed to the synth queue — Piper starts speaking it while the model is still
  generating the rest. So **time-to-first-audio ≈ (time to generate one
  sentence) + (time to synthesize one sentence)**, not the whole response.
- Subsequent sentences pipeline: by the time sentence A finishes playing,
  sentence B is usually already synthesized and waiting in the audio queue, so
  speech flows continuously.

In short, we trade "speak the whole thing perfectly later" for "start speaking
the first sentence now" — which is what makes a voice assistant feel responsive.

### Markdown & code handling (so it sounds human)
Before queuing, each sentence is cleaned:
- `clean_markdown()` strips `**`, `_`, `#`, backticks, `>` and converts
  `[label](url)` → `label`, so the voice never says "asterisk asterisk".
- **Code blocks** (```` ``` ````-fenced) are detected by the parser and **not
  read symbol-by-symbol**. Instead it counts the lines and speaks a short
  summary — *"Code block, 12 lines, shown on screen."* — while the full code
  still renders in the dashboard. Verified: a fenced `def f(): …` was summarized
  as a line count, never spoken.

---

## 5. Interruption: thread-safe and race-free

When the user presses the mic hotkey mid-sentence, speech must stop **now**.
Killing threads or sharing an Event across three stages invites races. Instead
we use a **generation counter**:

- Every queued sentence and every queued audio buffer is tagged with the
  current generation number.
- `interrupt()` takes a tiny lock, does `self._gen += 1`, then drains both
  queues and resets the parser.
- The synth and playback workers compare each item's tag to the *current*
  generation. Anything tagged with an old generation is **silently skipped** —
  it's been "orphaned." The playback loop also re-checks the generation
  **between audio blocks** (~90ms each), so the currently-playing sentence stops
  within one block, not at the end.

Why this is safe: the only shared mutable state touched during interruption is
one integer behind a lock. The worker threads never die or restart — they just
discard stale work and immediately pick up the next response. No thread-join
deadlocks, no half-closed audio streams, no lost wakeups. Verified: after
`interrupt()`, the generation advanced, both queues were empty, and queued
work was orphaned.

---

## 6. Files in this phase

```
tracker/
├── tts_engine.py        # NEW — SentenceBuffer + TTSEngine (synth + play threads)
└── config.py            # UPDATED — TTS_MODEL_PATH, sentence endings, block size
requirements.txt          # UPDATED — piper-tts>=1.2
tests/test_tracker.py     # UPDATED — 4 TTS tests (32 total, all pass)
en_US-lessac-medium.onnx  # the Piper voice model (+ .json) in project root
```

---

## 7. How to run

```bash
pip install -r requirements.txt        # piper-tts (+ onnxruntime)
# Voice model en_US-lessac-medium.onnx(+.json) must be in the project root.

# Live demo: stream an Ollama answer and speak it aloud:
python -m tracker.tts_engine "Explain a binary search briefly"

# In code:
from tracker.tts_engine import TTSEngine
tts = TTSEngine(); tts.start()
for token in ollama_stream:   # your /api/generate token loop
    tts.feed(token)
tts.flush()                   # speak the trailing partial sentence
# tts.interrupt()             # stop instantly (e.g. mic hotkey pressed)
```

---

## 8. What's next — the full voice loop

With 4.1 (capture) + 4.2 (transcribe) + 4.3 (speak), the loop is complete:
press the hotkey → speak → faster-whisper transcribes → Phase 3.3 mixer adds
your live code context → Ollama streams an answer → Piper speaks it while it
appears on screen → press the hotkey again to interrupt and ask a follow-up.
The natural next step is an orchestrator that wires these four modules together
inside the daemon.
