"""
voice_frontend.py — Phase 4.5 (refactor): the unified continuous audio engine.

ONE always-open 16kHz microphone stream serves all four orchestrator states.
The orchestrator switches the front-end's *mode*; each incoming 80ms frame is
routed accordingly on the PortAudio thread:

    MODE_STANDBY  — run openwakeword on every frame; on detection → on_wake().
    MODE_LISTEN   — energy VAD endpointing: wait for speech, capture until ~0.9s
                    of trailing silence (or max), then → on_utterance(wav).
                    If no speech begins within the idle timeout → on_utterance(None).
    MODE_GUARD    — (used during SPEAKING) watch for SUSTAINED voice above a
                    higher threshold; if heard → on_bargein() (barge-in).
    MODE_OFF      — ignore frames.

Keeping the mic open across states is what makes both no-repeat multi-turn and
barge-in possible: the orchestrator never has to tear down and reopen audio.
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import Callable, Optional

import numpy as np

from . import config

log = logging.getLogger("nexus.tracker.voice")

MODE_OFF = "off"
MODE_STANDBY = "standby"
MODE_LISTEN = "listen"
MODE_GUARD = "guard"


class VoiceFrontend:
    def __init__(
        self,
        on_wake: Callable[[], None],
        on_utterance: Callable[[Optional[io.BytesIO]], None],
        on_bargein: Callable[[], None],
        wake_model: str = config.WAKE_WORD_MODEL,
        wake_threshold: float = config.WAKE_THRESHOLD,
    ) -> None:
        self._on_wake = on_wake
        self._on_utterance = on_utterance
        self._on_bargein = on_bargein
        self._wake_model = wake_model
        self._wake_threshold = wake_threshold

        self._oww = None
        self._stream = None
        self._sr = config.AUDIO_SAMPLE_RATE
        self._frame = config.WAKE_FRAME_SAMPLES        # 1280 (80ms)
        self._fps = self._sr / self._frame             # frames per second (~12.5)

        self._mode = MODE_OFF
        self._lock = threading.Lock()

        # LISTEN state
        self._captured: list[np.ndarray] = []
        self._started = False
        self._silence = 0
        self._idle = 0
        self._listen_silence_limit = int(config.VAD_SILENCE_MS / 1000 * self._fps)
        self._listen_idle_limit = int(config.SESSION_IDLE_TIMEOUT_SEC * self._fps)
        self._listen_max_frames = int(config.VAD_MAX_UTTERANCE_SEC * self._sr)
        self._listen_frames = 0

        # GUARD state
        self._guard_voice = 0
        self._guard_sustain = int(config.BARGEIN_SUSTAIN_MS / 1000 * self._fps)

    # -- model / lifecycle --------------------------------------------------
    def _ensure_model(self):
        if self._oww is None:
            from openwakeword.model import Model
            log.info("loading wake-word model '%s'…", self._wake_model)
            self._oww = Model(wakeword_models=[self._wake_model],
                              inference_framework="onnx")
        return self._oww

    def start(self) -> None:
        import sounddevice as sd
        self._ensure_model()
        self._stream = sd.InputStream(
            samplerate=self._sr, channels=1, dtype="int16",
            blocksize=self._frame, callback=self._callback,
        )
        self._stream.start()
        log.info("voice front-end stream open.")

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop(); stream.close()
            except Exception:
                pass
        self.set_mode(MODE_OFF)

    # -- mode control (called from the asyncio thread) ----------------------
    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode
            if mode == MODE_LISTEN:
                self._captured = []
                self._started = False
                self._silence = 0
                self._idle = 0
                self._listen_frames = 0
            elif mode == MODE_GUARD:
                self._guard_voice = 0

    # -- audio callback (PortAudio thread) ----------------------------------
    @staticmethod
    def _rms(samples: np.ndarray) -> float:
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

    def _callback(self, indata, frames, time_info, status) -> None:
        samples = indata.reshape(-1).copy()
        with self._lock:
            mode = self._mode
            if mode == MODE_STANDBY:
                self._standby(samples)
            elif mode == MODE_LISTEN:
                self._listen(samples)
            elif mode == MODE_GUARD:
                self._guard(samples)

    def _standby(self, samples: np.ndarray) -> None:
        try:
            scores = self._oww.predict(samples)
        except Exception:
            return
        if scores and max(scores.values()) >= self._wake_threshold:
            self._mode = MODE_OFF  # stop reacting until orchestrator re-arms
            self._fire(self._on_wake)

    def _listen(self, samples: np.ndarray) -> None:
        rms = self._rms(samples)
        if not self._started:
            if rms >= config.VAD_START_RMS:
                self._started = True
                self._captured.append(samples)
                self._listen_frames += len(samples)
            else:
                self._idle += 1
                if self._idle >= self._listen_idle_limit:
                    self._mode = MODE_OFF
                    self._fire(lambda: self._on_utterance(None))  # nothing heard
            return

        # speech in progress
        self._captured.append(samples)
        self._listen_frames += len(samples)
        self._silence = self._silence + 1 if rms < config.VAD_START_RMS else 0
        if (self._silence >= self._listen_silence_limit
                or self._listen_frames >= self._listen_max_frames):
            wav = self._finish()
            self._mode = MODE_OFF
            self._fire(lambda: self._on_utterance(wav))

    def _guard(self, samples: np.ndarray) -> None:
        rms = self._rms(samples)
        self._guard_voice = self._guard_voice + 1 if rms >= config.BARGEIN_RMS else 0
        if self._guard_voice >= self._guard_sustain:
            self._mode = MODE_OFF
            self._fire(self._on_bargein)

    # -- helpers ------------------------------------------------------------
    def _finish(self) -> Optional[io.BytesIO]:
        if not self._captured:
            return None
        audio = np.concatenate(self._captured, axis=0)
        self._captured = []
        if audio.size < self._sr // 4:  # < 0.25s
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(self._sr)
            wav.writeframes(audio.astype(np.int16).tobytes())
        buf.seek(0); buf.name = "utterance.wav"
        return buf

    @staticmethod
    def _fire(fn) -> None:
        try:
            fn()
        except Exception as exc:
            log.warning("voice callback failed: %s", exc)
