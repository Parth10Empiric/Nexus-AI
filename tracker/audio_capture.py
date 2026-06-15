"""
audio_capture.py — Phase 4.1 "Local Microphone Stream Capturer".

A system-wide Push-to-Talk recorder. Hold the global hotkey (default
Ctrl+Space) from anywhere — even with the desktop UI minimized — speak, then
release. The captured audio is flushed to an in-memory WAV buffer (never the
disk) in exactly the format faster-whisper wants: 16kHz, mono, 16-bit PCM.

Three threads cooperate, none blocking the main daemon:
  * MAIN thread        — your existing window tracker / watchdog loops.
  * LISTENER thread    — pynput keyboard listener (owned by pynput).
  * PORTAUDIO thread   — sounddevice's input callback fires here.

The recorder is deliberately decoupled from the hotkey controller, so you can
drive it from a UI button or a unit test just as easily as from the keyboard.

Run standalone:   python -m tracker.audio_capture
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from . import config

log = logging.getLogger("nexus.tracker.audio")


class AudioRecorder:
    """
    Thread-safe microphone recorder producing an in-memory WAV buffer.

    Usage:
        rec = AudioRecorder()
        rec.start()
        ... (audio accumulates on the PortAudio callback thread) ...
        wav = rec.stop()          # -> io.BytesIO positioned at 0, or None
    """

    def __init__(
        self,
        sample_rate: int = config.AUDIO_SAMPLE_RATE,
        channels: int = config.AUDIO_CHANNELS,
        dtype: str = config.AUDIO_DTYPE,
        blocksize: int = config.AUDIO_BLOCKSIZE,
        max_seconds: int = config.AUDIO_MAX_SECONDS,
        device: Optional[int] = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._dtype = dtype
        self._blocksize = blocksize
        self._max_frames = max_seconds * sample_rate
        self._device = device

        self._stream: Optional[sd.InputStream] = None
        self._frames: list[np.ndarray] = []
        self._frame_count = 0
        self._recording = False
        self._lock = threading.Lock()  # guards _frames / _frame_count / _recording

    # -- PortAudio callback (runs on its own thread) ------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            # Over/underflows are recoverable; just note them.
            log.debug("audio status: %s", status)
        with self._lock:
            if not self._recording:
                return
            if self._frame_count >= self._max_frames:
                # Safety cap reached — stop accumulating (prevents runaway RAM).
                return
            # indata is reused by PortAudio after this returns, so COPY it.
            self._frames.append(indata.copy())
            self._frame_count += frames

    # -- control ------------------------------------------------------------
    def start(self) -> bool:
        """Begin recording. Returns False if already recording or stream fails."""
        with self._lock:
            if self._recording:
                return False
            self._frames = []
            self._frame_count = 0
            self._recording = True

        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype=self._dtype,
                blocksize=self._blocksize,
                device=self._device,
                callback=self._callback,
            )
            self._stream.start()
            log.info("🎙️  recording started (%d Hz, %d ch, %s)",
                     self._sample_rate, self._channels, self._dtype)
            return True
        except Exception as exc:
            log.error("failed to open input stream: %s", exc)
            with self._lock:
                self._recording = False
            self._close_stream()
            return False

    def stop(self) -> Optional[io.BytesIO]:
        """
        Stop recording and return the audio as an in-memory WAV buffer
        (BytesIO, seeked to 0). Returns None if nothing was captured.
        """
        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            frames = self._frames
            self._frames = []  # release references so memory can be reclaimed
            count = self._frame_count
            self._frame_count = 0

        self._close_stream()

        if not frames:
            log.info("recording stopped: no audio captured.")
            return None

        # One contiguous array, then free the per-block list immediately.
        audio = np.concatenate(frames, axis=0)
        frames.clear()

        duration = count / self._sample_rate
        log.info("recording stopped: %.2fs (%d frames).", duration, count)
        return self._to_wav_buffer(audio)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log.debug("stream close error: %s", exc)

    # -- WAV export ---------------------------------------------------------
    def _to_wav_buffer(self, audio: np.ndarray) -> io.BytesIO:
        """Encode an int16 numpy array into a WAV stream in memory."""
        # Ensure the dtype/shape the WAV header will claim.
        if audio.dtype != np.int16:
            audio = audio.astype(np.int16)
        if audio.ndim > 1 and self._channels == 1:
            audio = audio.reshape(-1)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(self._channels)
            wav.setsampwidth(2)  # 16-bit PCM = 2 bytes/sample
            wav.setframerate(self._sample_rate)
            wav.writeframes(audio.tobytes())
        buffer.seek(0)
        buffer.name = "speech.wav"  # some consumers look for a name attr
        return buffer

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording


class PushToTalkController:
    """
    Global Ctrl+Space push-to-talk. Holds the chord -> records; release -> fires
    `on_audio(wav_buffer)` on the listener thread.

    The pynput listener runs on its own thread, so this never blocks the main
    daemon. Auto-repeat key events while holding are ignored via the recorder's
    own `is_recording` guard.
    """

    def __init__(self, on_audio: Callable[[io.BytesIO], None],
                 recorder: Optional[AudioRecorder] = None,
                 on_record_start: Optional[Callable[[], None]] = None) -> None:
        from pynput import keyboard  # imported lazily so headless tests can skip

        self._keyboard = keyboard
        self._on_audio = on_audio
        self._on_record_start = on_record_start  # fires when a press begins recording
        self._recorder = recorder or AudioRecorder()
        self._listener: Optional["keyboard.Listener"] = None

        self._modifier_down = False
        self._key_down = False
        self._state_lock = threading.Lock()

    # -- key matching -------------------------------------------------------
    def _is_modifier(self, key) -> bool:
        kb = self._keyboard.Key
        wanted = config.PTT_MODIFIER
        if wanted == "ctrl":
            return key in (kb.ctrl_l, kb.ctrl_r, kb.ctrl)
        if wanted == "alt":
            return key in (kb.alt_l, kb.alt_r, kb.alt)
        if wanted == "shift":
            return key in (kb.shift, kb.shift_l, kb.shift_r)
        return False

    def _is_main_key(self, key) -> bool:
        kb = self._keyboard.Key
        if config.PTT_KEY == "space":
            return key == kb.space
        # Single character key (e.g. "k")
        return getattr(key, "char", None) == config.PTT_KEY

    # -- callbacks (listener thread) ----------------------------------------
    def _on_press(self, key) -> None:
        changed = False
        with self._state_lock:
            if self._is_modifier(key) and not self._modifier_down:
                self._modifier_down = True
                changed = True
            elif self._is_main_key(key) and not self._key_down:
                self._key_down = True
                changed = True
            should_start = self._modifier_down and self._key_down
        if changed and should_start and not self._recorder.is_recording:
            # Fire the press hook FIRST (barge-in: interrupt any current speech)
            # so the new recording isn't polluted by the assistant's own audio.
            if self._on_record_start is not None:
                try:
                    self._on_record_start()
                except Exception as exc:
                    log.warning("on_record_start handler failed: %s", exc)
            self._recorder.start()

    def _on_release(self, key) -> None:
        with self._state_lock:
            released_chord_key = False
            if self._is_modifier(key) and self._modifier_down:
                self._modifier_down = False
                released_chord_key = True
            elif self._is_main_key(key) and self._key_down:
                self._key_down = False
                released_chord_key = True
            was_recording = self._recorder.is_recording

        if released_chord_key and was_recording:
            wav = self._recorder.stop()
            if wav is not None:
                try:
                    self._on_audio(wav)
                except Exception as exc:
                    log.warning("on_audio handler failed: %s", exc)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._listener = self._keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("push-to-talk armed: hold %s+%s to record.",
                 config.PTT_MODIFIER.upper(), config.PTT_KEY.upper())

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._recorder.is_recording:
            self._recorder.stop()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    def handle(wav: io.BytesIO) -> None:
        size = len(wav.getbuffer())
        log.info("got %d bytes of WAV audio in memory (ready for Whisper).", size)

    controller = PushToTalkController(on_audio=handle)
    controller.start()
    log.info("Press Ctrl+C to quit.")
    try:
        threading.Event().wait()  # park the main thread without busy-looping
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        log.info("push-to-talk stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
