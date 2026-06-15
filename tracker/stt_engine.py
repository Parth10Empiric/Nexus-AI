"""
stt_engine.py — Phase 4.2 "Local Speech-to-Text Processing".

Turns the in-memory WAV buffer from Phase 4.1 into a clean English string,
entirely on the local CPU, using faster-whisper (a CTranslate2 reimplementation
of OpenAI Whisper that is several times faster and lighter on CPU).

Tuned for the i5-6500 (4 cores, no GPU):
  * model = "base.en"   — English-only, best speed/accuracy balance on CPU
  * device = "cpu"
  * compute_type = "int8" — 8-bit quantization: <1GB RAM, max CPU throughput
  * cpu_threads = 4       — one per physical core
  * vad_filter = True     — drops silence/breath so the model does less work

The model is loaded ONCE (lazily) and reused across calls — loading is the
expensive part, so we never pay it per transcription.

Run standalone:   python -m tracker.stt_engine path/to/audio.wav
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Optional, Union

from . import config

log = logging.getLogger("nexus.tracker.stt")

AudioInput = Union[io.BytesIO, str]


class WhisperTranscriber:
    """
    Thread-safe, lazily-initialized faster-whisper transcriber.

    The model is heavy to construct (~150MB int8 weights), so we build it on
    first use and guard construction with a lock — multiple threads calling
    transcribe() concurrently will share the single model instance.
    """

    def __init__(
        self,
        model_size: str = config.STT_MODEL_SIZE,
        device: str = config.STT_DEVICE,
        compute_type: str = config.STT_COMPUTE_TYPE,
        cpu_threads: int = config.STT_CPU_THREADS,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads

        self._model = None  # built lazily
        self._load_lock = threading.Lock()

    # -- model lifecycle ----------------------------------------------------
    def _ensure_model(self):
        """Build the WhisperModel once, with all CPU optimizations applied."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:  # double-checked under the lock
                from faster_whisper import WhisperModel

                log.info(
                    "loading Whisper '%s' (device=%s, compute=%s, threads=%d)…",
                    self._model_size, self._device, self._compute_type,
                    self._cpu_threads,
                )
                self._model = WhisperModel(
                    self._model_size,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                )
                log.info("Whisper model ready.")
        return self._model

    def warmup(self) -> None:
        """Optionally pre-load the model (e.g. at daemon startup) so the first
        real transcription isn't slowed by model construction."""
        self._ensure_model()

    # -- transcription ------------------------------------------------------
    def transcribe(self, audio: AudioInput, language: str = "en") -> str:
        """
        Transcribe an in-memory WAV buffer (or a path) to a clean string.

        faster-whisper accepts a file-like object directly, so we hand it the
        BytesIO without ever touching disk. VAD filtering trims silence to cut
        CPU work. Returns "" on empty/failed input rather than raising.
        """
        if audio is None:
            return ""

        # Make sure a BytesIO is rewound so the decoder reads from the start.
        if isinstance(audio, io.BytesIO):
            try:
                audio.seek(0)
            except (ValueError, OSError):
                return ""

        try:
            model = self._ensure_model()
        except Exception as exc:
            log.error("could not load Whisper model: %s", exc)
            return ""

        try:
            segments, info = model.transcribe(
                audio,
                language=language,
                vad_filter=True,                       # strip silence/breath
                vad_parameters={"min_silence_duration_ms": 500},
                beam_size=config.STT_BEAM_SIZE,
                condition_on_previous_text=False,      # avoids drift on short clips
            )
            # `segments` is a generator — consuming it runs the inference.
            text = " ".join(seg.text.strip() for seg in segments).strip()
            # Collapse any double spaces produced by joining.
            text = " ".join(text.split())
            log.info("transcribed %.2fs of speech -> %d chars",
                     getattr(info, "duration", 0.0), len(text))
            return text
        except Exception as exc:
            log.error("transcription failed: %s", exc)
            return ""


# Module-level singleton + convenience entry point ---------------------------
_default_transcriber: Optional[WhisperTranscriber] = None
_singleton_lock = threading.Lock()


def get_transcriber() -> WhisperTranscriber:
    """Return the shared transcriber (built once for the whole process)."""
    global _default_transcriber
    if _default_transcriber is None:
        with _singleton_lock:
            if _default_transcriber is None:
                _default_transcriber = WhisperTranscriber()
    return _default_transcriber


def transcribe_audio(buffer: AudioInput) -> str:
    """
    Phase 4.2 entry point. Accepts the in-memory WAV buffer produced by
    Phase 4.1's recorder and returns the final concatenated English string.
    """
    return get_transcriber().transcribe(buffer)


def main() -> int:
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    if len(sys.argv) < 2:
        print("usage: python -m tracker.stt_engine <audio.wav>")
        return 2
    with open(sys.argv[1], "rb") as fh:
        buf = io.BytesIO(fh.read())
    print("TRANSCRIPT:", transcribe_audio(buf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
