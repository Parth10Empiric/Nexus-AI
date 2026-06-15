"""
wake_word.py — Phase 4.5 "Local Wake Word Engine".

Replaces push-to-talk with hands-free activation. While armed, it listens to
the mic passively at very low CPU using `openwakeword` (a tiny ONNX model). It
runs the heavy `faster-whisper` transcription ONLY after it hears the wake
phrase — so background noise and idle time cost almost nothing.

One sounddevice InputStream feeds a small state machine:
    PASSIVE   — run openwakeword on each 80ms frame; on detection → CAPTURING.
    CAPTURING — accumulate frames until ~0.9s of trailing silence (or max
                duration), then emit the utterance as an in-memory WAV and
                return to PASSIVE.

The orchestrator supplies callbacks: on_wake() (fired the instant the phrase is
detected, e.g. to show "Listening") and on_utterance(wav) (the captured audio).
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import Callable, Optional

import numpy as np

from . import config

log = logging.getLogger("nexus.tracker.wake")

_PASSIVE = "passive"
_CAPTURING = "capturing"


class WakeWordListener:
    def __init__(
        self,
        on_wake: Callable[[], None],
        on_utterance: Callable[[io.BytesIO], None],
        model_name: str = config.WAKE_WORD_MODEL,
        threshold: float = config.WAKE_THRESHOLD,
    ) -> None:
        self._on_wake = on_wake
        self._on_utterance = on_utterance
        self._model_name = model_name
        self._threshold = threshold

        self._oww = None
        self._stream = None
        self._sample_rate = config.AUDIO_SAMPLE_RATE  # 16kHz
        self._frame = config.WAKE_FRAME_SAMPLES        # 1280 (80ms)

        self._state = _PASSIVE
        self._lock = threading.Lock()

        # Capture buffers / endpointing counters.
        self._captured: list[np.ndarray] = []
        self._silence_frames = 0
        self._captured_frames = 0
        self._silence_limit = int(
            config.WAKE_ENDPOINT_SILENCE_MS / 1000 * self._sample_rate / self._frame
        )
        self._max_frames = int(config.WAKE_MAX_UTTERANCE_SEC * self._sample_rate)

    # -- model --------------------------------------------------------------
    def _ensure_model(self):
        if self._oww is None:
            from openwakeword.model import Model
            log.info("loading wake-word model '%s'…", self._model_name)
            self._oww = Model(
                wakeword_models=[self._model_name],
                inference_framework="onnx",
            )
        return self._oww

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        import sounddevice as sd

        self._ensure_model()
        self._reset_capture()
        self._state = _PASSIVE
        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._frame,
            callback=self._callback,
        )
        self._stream.start()
        log.info("wake listener armed (say the wake word).")

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._reset_capture()
        self._state = _PASSIVE
        log.info("wake listener stopped.")

    # -- audio callback (PortAudio thread) ----------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("wake audio status: %s", status)
        samples = indata.reshape(-1).copy()  # mono int16

        with self._lock:
            if self._state == _PASSIVE:
                self._passive_frame(samples)
            else:
                self._capturing_frame(samples)

    def _passive_frame(self, samples: np.ndarray) -> None:
        try:
            scores = self._oww.predict(samples)
        except Exception as exc:
            log.debug("wake predict error: %s", exc)
            return
        score = max(scores.values()) if scores else 0.0
        if score >= self._threshold:
            log.info("🔔 wake word detected (score=%.2f).", score)
            self._state = _CAPTURING
            self._reset_capture()
            # Notify outside the lock-sensitive path is fine; callback is short.
            try:
                self._on_wake()
            except Exception as exc:
                log.warning("on_wake handler failed: %s", exc)

    def _capturing_frame(self, samples: np.ndarray) -> None:
        self._captured.append(samples)
        self._captured_frames += len(samples)

        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if samples.size else 0.0
        if rms < config.WAKE_SILENCE_RMS:
            self._silence_frames += 1
        else:
            self._silence_frames = 0

        ended = (
            self._silence_frames >= self._silence_limit
            or self._captured_frames >= self._max_frames
        )
        if ended:
            wav = self._finish_capture()
            self._state = _PASSIVE
            if wav is not None:
                try:
                    self._on_utterance(wav)
                except Exception as exc:
                    log.warning("on_utterance handler failed: %s", exc)

    # -- capture helpers ----------------------------------------------------
    def _reset_capture(self) -> None:
        self._captured = []
        self._silence_frames = 0
        self._captured_frames = 0

    def _finish_capture(self) -> Optional[io.BytesIO]:
        if not self._captured:
            return None
        audio = np.concatenate(self._captured, axis=0)
        self._reset_capture()
        if audio.size < self._sample_rate // 4:  # < 0.25s -> almost certainly noise
            return None

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._sample_rate)
            wav.writeframes(audio.astype(np.int16).tobytes())
        buffer.seek(0)
        buffer.name = "utterance.wav"
        return buffer
