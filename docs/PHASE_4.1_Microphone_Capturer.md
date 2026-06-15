# Nexus AI — Phase 4.1: Local Microphone Stream Capturer

> A learning guide. Plain-language explanation of **what** we built, **why**
> each tool, and **how** the threading, buffering, and 16kHz format work.

---

## 1. What is this phase, in one sentence?

We built a system-wide **push-to-talk** recorder: hold Ctrl+Space from anywhere
(even with the app minimized), speak, release — and your voice lands in an
in-memory WAV buffer, already in the exact 16kHz/mono/16-bit format the next
step (faster-whisper) needs.

---

## 2. The big picture (three threads, none blocking)

```
  MAIN thread            LISTENER thread (pynput)        PORTAUDIO thread
  ───────────            ────────────────────────        ─────────────────
  window tracker         hears Ctrl+Space DOWN ─▶ recorder.start()
  + watchdog                                              InputStream opens
  keep running                                            _callback() fires
  undisturbed                                             every 100ms:
                                                            append indata.copy()
                         hears key UP ─▶ recorder.stop()
                              │           concat frames -> WAV in RAM
                              ▼
                         on_audio(wav_buffer)  ──▶  (Step 4.2: transcribe)
```

The recorder and the hotkey controller are separate classes, so the same
recorder can be driven by a UI button or a unit test, not just the keyboard.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`sounddevice`** | Thin Python binding over **PortAudio**. Gives a callback-based `InputStream` that hands us raw audio frames directly in the dtype we ask for (`int16`) — no conversion needed. |
| **`numpy`** | Audio frames are numpy arrays. Concatenating blocks and converting to bytes is fast and zero-copy-ish. |
| **`pynput`** | A **global** keyboard listener — it sees Ctrl+Space even when our window isn't focused, which is what "system-wide push-to-talk" requires. |
| **`wave` + `io.BytesIO` (stdlib)** | Write a valid WAV file **into memory** instead of to disk — no I/O, instant hand-off to the transcriber. |
| **`portaudio19-dev`, `ffmpeg` (apt)** | System libs PortAudio/decoders build on; already installed. |

---

## 4. The 16kHz / mono / 16-bit format — and why it saves CPU

We open the stream with `samplerate=16000, channels=1, dtype="int16"`. This is
**not** an arbitrary choice — Whisper models are trained on 16kHz mono audio.
Capturing natively in that format means **zero resampling later**. Here's the
saving versus recording at CD quality (44.1kHz stereo):

- **Sample rate 44.1kHz → 16kHz** is ~2.76× fewer samples per second. Recording
  at 44.1kHz would force a resample step (an FIR/polyphase filter over every
  sample) before Whisper — pure wasted CPU on a GPU-less i5.
- **Stereo → mono** halves the data again and avoids a downmix step.
- **float32 → int16** halves bytes-per-sample (4 → 2) and matches what the WAV
  PCM container and Whisper's front-end expect.

Net: 44.1kHz stereo float32 is `44100 × 2 × 4 = 352,800 bytes/sec`; our format
is `16000 × 1 × 2 = 32,000 bytes/sec` — **~11× less data** flowing through the
callback, the buffer, and the encoder, and **no resample/downmix/convert
passes**. On a CPU-only machine that's the difference between instant and
laggy transcription.

---

## 5. How buffer management prevents memory leaks

Long or repeated recordings are where naive audio code leaks. Four safeguards:

1. **Copy-on-capture.** PortAudio reuses its `indata` buffer after the callback
   returns, so we store `indata.copy()`. Without the copy we'd hold references
   into a buffer PortAudio overwrites — corruption, not a leak, but equally
   fatal. The copy makes each block independently owned.
2. **List-of-blocks during capture, concatenated once at stop.** We append small
   100ms int16 blocks to a Python list while recording, then do a single
   `np.concatenate` at `stop()`. We never repeatedly grow/reallocate one big
   array mid-stream.
3. **Explicit release after flush.** On `stop()` we reassign `self._frames = []`
   (dropping all block references), then after concatenating call
   `frames.clear()`. Both the per-block list and its contents become eligible
   for garbage collection immediately — the recorder holds no audio between
   sessions.
4. **A hard safety cap.** `AUDIO_MAX_SECONDS` (120s) caps `_frame_count`; once
   reached, the callback stops appending. So a stuck/held key — or a forgotten
   hot mic — can never grow RAM without bound. At 32KB/s even the full cap is
   only ~3.8 MB. **Verified:** pushing 5s of audio into a 1s-capped recorder
   stopped accumulating at exactly the limit.

All of this is guarded by a single `threading.Lock`, so the PortAudio thread
(appending) and the listener thread (start/stop) never race on `_frames`.

---

## 6. Thread safety (not blocking the daemon)

- **pynput listener** runs on its own daemon thread — it owns key events.
- **sounddevice callback** fires on PortAudio's internal thread — it owns frame
  capture.
- The **main thread** (window tracker + watchdog from Phases 1–3) is never
  touched; it keeps polling and watching files.
- Shared state (`_frames`, `_frame_count`, `_recording`) is protected by a lock;
  the push-to-talk chord state has its own lock. Key auto-repeat (which fires
  `on_press` repeatedly while held) is ignored via the `is_recording` guard, so
  one hold = one recording. **Verified** by the state-machine test.

---

## 7. Files in this phase

```
tracker/
├── audio_capture.py     # NEW — AudioRecorder + PushToTalkController
└── config.py            # UPDATED — AUDIO_* + PTT_* settings
requirements.txt          # UPDATED — sounddevice, numpy, pynput (+ apt note)
tests/test_tracker.py     # UPDATED — 4 audio tests (24 total, all pass)
```

---

## 8. How to run

```bash
# OS libs (already installed on your machine):
sudo apt install portaudio19-dev ffmpeg
pip install -r requirements.txt        # sounddevice, numpy, pynput

# Standalone demo:
python -m tracker.audio_capture
# Hold Ctrl+Space, speak, release -> logs "got N bytes of WAV audio in memory".
```

Verified live: a real 0.5s capture from the default input device produced a
valid in-memory WAV; synthetic frames produced a correct 16kHz/mono/16-bit
header.

---

## 9. What's next (Phase 4.2)

The `on_audio(wav_buffer)` callback is the seam. Step 4.2 feeds that BytesIO
straight into **faster-whisper** for local transcription — no temp files, no
resampling — then the transcribed text flows into the Phase 3.3 prompt mixer
and on to Ollama, giving you fully voice-driven, context-aware code help.
