# Nexus AI — Phase 4.2: Local Speech-to-Text (faster-whisper)

> A learning guide. Plain-language explanation of **what** we built, **why**
> each setting, and **how** the CPU optimizations make it fast.

---

## 1. What is this phase, in one sentence?

We turned the in-memory voice recording from Phase 4.1 into a clean English
text string — transcribed entirely on the local CPU in about a second, with no
GPU, no cloud, and no temp files.

---

## 2. The flow

```
  Phase 4.1 push-to-talk ──▶ io.BytesIO (16kHz mono 16-bit WAV, in RAM)
                                   │
                                   ▼  transcribe_audio(buffer)
                         WhisperTranscriber (faster-whisper)
                            base.en · cpu · int8 · 4 threads · VAD
                                   │
                                   ▼
                         "fix the function that divides by zero"
                                   │
                                   ▼  (Phase 3.3 mixer → Ollama)
```

The model loads **once** (lazily, behind a lock) and is reused for every
utterance — loading is the slow part, so we never pay it per transcription.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`faster-whisper`** | A reimplementation of OpenAI Whisper on **CTranslate2**. Same models, but 4× faster and far lighter on CPU, with native int8 support. |
| **`CTranslate2`** | The inference engine underneath — optimized C++ with quantization, threading, and CPU SIMD (AVX2) kernels. |
| **`base.en` model** | English-only "base" tier — the sweet spot of accuracy vs speed on a CPU. |
| **VAD filter (built in)** | Voice Activity Detection trims silence/breath before inference, so the model processes only actual speech. |
| **`io.BytesIO`** | faster-whisper accepts a file-like object, so we transcribe straight from RAM — no disk. |

---

## 4. The critical CPU configuration

```python
WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)
# transcribe(..., vad_filter=True, beam_size=1, condition_on_previous_text=False)
```

| Setting | What it does | Why it matters here |
|---|---|---|
| `base.en` | English-only base model (~74M params) | Bigger models (small/medium) are far slower on CPU for marginal accuracy gains; `.en` drops multilingual weight we don't need. |
| `device="cpu"` | Run on the i5, not a GPU | There is no GPU; this is mandatory. |
| `compute_type="int8"` | 8-bit weight quantization | Cuts memory to <1GB and lets the CPU do integer math (faster than float) — the single biggest speed lever. |
| `cpu_threads=4` | One thread per physical core | Saturates the i5-6500's 4 cores without oversubscription/thrashing. |
| `vad_filter=True` | Skip silence | Less audio in = less inference = faster; also removes "hallucinated" words from background hum. |
| `beam_size=1` | Greedy decode | Fastest; short command-style utterances rarely need beam search. |
| `condition_on_previous_text=False` | Don't carry prior context | Prevents drift/repetition on short, independent clips. |

**Verified:** real synthesized speech transcribed in ~1s per short phrase on the
CPU; an empty/garbage buffer returns `""` without crashing.

---

## 5. Why base.en + int8 beats standard OpenAI Whisper on a 4-core CPU

Two independent wins — a better **engine** and a cheaper **numeric format**:

**Engine (CTranslate2 vs PyTorch).** Stock `openai-whisper` runs on PyTorch,
whose CPU path is general-purpose and carries Python/eager-mode overhead per
operation. faster-whisper runs the same model on **CTranslate2**, a C++ engine
purpose-built for transformer inference: it fuses operations, uses cache-aware
memory layouts, exploits CPU SIMD (AVX2) kernels, and threads cleanly across
cores. On CPU this is typically **~4× faster at a fraction of the RAM** for
identical output quality.

**Numeric format (int8 vs float32).** Standard Whisper computes in float32 (4
bytes/weight). `compute_type="int8"` quantizes weights to 8-bit integers:
- **Memory**: ~4× smaller weights — `base.en` fits comfortably **under 1GB**,
  versus float32 spilling cache and pressuring the 16GB shared with Ollama.
- **Speed**: integer matrix multiplies run faster than float on a CPU, and the
  smaller weights mean far less memory bandwidth — usually the real bottleneck
  on CPU inference. More of the model stays in L2/L3 cache.
- **Accuracy**: int8 quantization costs only a tiny WER increase, negligible for
  short spoken commands.

**Thread fit.** `cpu_threads=4` maps exactly to the i5-6500's 4 physical cores —
full parallelism without the context-switch thrash that oversubscription causes.

Net: where stock float32 OpenAI Whisper might take many seconds and ~2–3GB for
`base` on this machine, faster-whisper `base.en` int8 transcribes a short
command in about a second under 1GB — the difference between a usable voice
assistant and an unusable one on GPU-less hardware.

---

## 6. Files in this phase

```
tracker/
├── stt_engine.py        # NEW — WhisperTranscriber + transcribe_audio()
└── config.py            # UPDATED — STT_MODEL_SIZE/DEVICE/COMPUTE_TYPE/CPU_THREADS
requirements.txt          # UPDATED — faster-whisper>=1.0
tests/test_tracker.py     # UPDATED — 4 STT tests (28 total, all pass)
```

---

## 7. How to run

```bash
pip install -r requirements.txt        # faster-whisper (+ ctranslate2)
# First run downloads base.en (~150MB int8) once, then it's cached.

# Standalone on a WAV file:
python -m tracker.stt_engine some_recording.wav

# In code (the Phase 4.1 hand-off):
from tracker.stt_engine import transcribe_audio
text = transcribe_audio(wav_buffer)    # wav_buffer from AudioRecorder.stop()
```

---

## 8. What's next (Phase 4.3)

The transcribed text becomes a chat query: feed it into the Phase 3.3 prompt
mixer (live code context) → Ollama → and in Phase 4.3, pipe the streamed answer
into **Piper TTS** so the fix is spoken aloud. The full voice loop closes there.
