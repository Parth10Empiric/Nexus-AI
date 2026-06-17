"""
tts_engine.py — Phase 4.3 "Local Text-to-Speech Output Pipeline" (Piper).

Speaks Ollama's streamed answer aloud in real time, sentence-by-sentence, on
the local CPU — no cloud, low latency, fully interruptible.

The pipeline has three decoupled stages so the mouth never waits for the brain:

    feed(token)  ──▶  SentenceBuffer  ──▶  text queue
                       (parse + clean + code-block summary)
                                              │
                          synth thread  ──────┘  Piper(sentence) -> int16 bytes
                                              │
                          audio queue  ───────┘
                                              │
                          playback thread ────┘  sounddevice OutputStream

While sentence A plays, the synth thread is already compiling sentence B.

Interruption (mic hotkey pressed again) is race-free via a generation counter:
bumping it instantly orphans everything in flight without killing the threads.

Run a live demo (streams from Ollama and speaks):
    python -m tracker.tts_engine "Explain a binary search briefly"
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Iterable, Optional

import numpy as np

from . import config

log = logging.getLogger("nexus.tracker.tts")

_SENTINEL = object()  # poison pill to unblock the worker queues on shutdown


# ---------------------------------------------------------------------------
# Streaming token parser: tokens -> clean, speakable sentences
# ---------------------------------------------------------------------------
class SentenceBuffer:
    """
    Accumulates streamed tokens and emits complete, cleaned sentences via the
    `emit` / `emit_code` callbacks. Handles Markdown stripping and code blocks.

    Not thread-safe on its own — the engine only ever feeds it from one thread.
    """

    _FENCE = "```"

    def __init__(self, emit, emit_code, endings: str = config.TTS_SENTENCE_ENDINGS):
        self._emit = emit             # called with a cleaned sentence string
        self._emit_code = emit_code   # called with a line count
        self._endings = set(endings)
        self._buf = ""
        self._in_code = False
        self._code_lines = 0

    def reset(self) -> None:
        self._buf = ""
        self._in_code = False
        self._code_lines = 0

    def feed(self, token: str) -> None:
        self._buf += token
        self._consume(final=False)

    def flush(self) -> None:
        """End of stream: emit whatever remains."""
        self._consume(final=True)

    # -- internals ----------------------------------------------------------
    def _consume(self, final: bool) -> None:
        progress = True
        while progress:
            progress = False

            if self._in_code:
                idx = self._buf.find(self._FENCE)
                if idx != -1:  # closing fence found
                    self._code_lines += self._buf[:idx].count("\n")
                    self._buf = self._buf[idx + len(self._FENCE):]
                    self._in_code = False
                    self._flush_code()
                    progress = True
                elif final and self._buf:
                    self._code_lines += self._buf.count("\n")
                    self._buf = ""
                    self._in_code = False
                    self._flush_code()
                # else: still inside an open code block, wait for more tokens
                continue

            fence = self._buf.find(self._FENCE)
            end = self._first_sentence_end()

            if fence != -1 and (end == -1 or fence < end):
                # Text before the fence is a fragment; speak it, then enter code.
                pre = self._buf[:fence]
                self._buf = self._buf[fence + len(self._FENCE):]
                self._in_code = True
                self._code_lines = 0
                if pre.strip():
                    self._emit_clean(pre)
                progress = True
            elif end != -1:
                sentence = self._buf[: end + 1]
                self._buf = self._buf[end + 1:]
                if sentence.strip():
                    self._emit_clean(sentence)
                progress = True
            elif final and self._buf.strip():
                self._emit_clean(self._buf)
                self._buf = ""

    def _first_sentence_end(self) -> int:
        for i, ch in enumerate(self._buf):
            if ch in self._endings:
                return i
        return -1

    def _flush_code(self) -> None:
        if config.TTS_SUMMARIZE_CODE_BLOCKS:
            self._emit_code(max(self._code_lines, 1))
        self._code_lines = 0

    def _emit_clean(self, raw: str) -> None:
        text = clean_markdown(raw)
        if text:
            self._emit(text)


# Strip Markdown so the voice doesn't pronounce syntax. ----------------------
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_INLINE_TICKS_RE = re.compile(r"`+")
_EMPHASIS_RE = re.compile(r"[*_#>~]+")
_WS_RE = re.compile(r"\s+")


def clean_markdown(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)      # [label](url) -> label
    text = _INLINE_TICKS_RE.sub("", text)  # `code` ticks
    text = _EMPHASIS_RE.sub("", text)      # ** _ # > ~
    text = _WS_RE.sub(" ", text).strip()
    return text


# ---------------------------------------------------------------------------
# The engine: synth thread + playback thread + interrupt
# ---------------------------------------------------------------------------
class TTSEngine:
    def __init__(self, model_path=config.TTS_MODEL_PATH):
        self._model_path = str(model_path)
        self._voice = None
        self._sample_rate = 22050  # overwritten from the model on load

        self._text_q: "queue.Queue" = queue.Queue()
        self._audio_q: "queue.Queue" = queue.Queue()

        self._gen = 0  # generation counter for race-free interruption
        self._gen_lock = threading.Lock()

        self._shutdown = threading.Event()
        self._synth_thread: Optional[threading.Thread] = None
        self._play_thread: Optional[threading.Thread] = None

        self._active = False  # True while a sentence is actively playing
        self._active_lock = threading.Lock()

        self._buffer = SentenceBuffer(self._on_sentence, self._on_code)

    @property
    def is_speaking(self) -> bool:
        """True if anything is queued OR a sentence is currently playing."""
        with self._active_lock:
            active = self._active
        return active or not self._text_q.empty() or not self._audio_q.empty()

    # -- lifecycle ----------------------------------------------------------
    def _load_voice(self):
        if self._voice is None:
            from piper import PiperVoice
            log.info("loading Piper voice: %s", self._model_path)
            self._voice = PiperVoice.load(self._model_path)
            # AudioChunk carries the real sample rate; grab it from a tiny synth.
            try:
                first = next(iter(self._voice.synthesize("ready")))
                self._sample_rate = first.sample_rate
            except Exception:
                pass
        return self._voice

    def start(self) -> None:
        self._load_voice()
        self._shutdown.clear()
        self._synth_thread = threading.Thread(
            target=self._synth_loop, name="tts-synth", daemon=True)
        self._play_thread = threading.Thread(
            target=self._play_loop, name="tts-play", daemon=True)
        self._synth_thread.start()
        self._play_thread.start()
        log.info("TTS engine started (sample_rate=%d Hz).", self._sample_rate)

    def stop(self) -> None:
        self._shutdown.set()
        self._text_q.put(_SENTINEL)
        self._audio_q.put(_SENTINEL)
        for t in (self._synth_thread, self._play_thread):
            if t is not None:
                t.join(timeout=3)
        log.info("TTS engine stopped.")

    # -- producer API -------------------------------------------------------
    def _current_gen(self) -> int:
        with self._gen_lock:
            return self._gen

    def feed(self, token: str) -> None:
        """Feed one streamed token from Ollama."""
        self._buffer.feed(token)

    def feed_stream(self, tokens: Iterable[str]) -> None:
        for tok in tokens:
            self.feed(tok)
        self.flush()

    def flush(self) -> None:
        """End of the LLM stream: speak any trailing partial sentence."""
        self._buffer.flush()

    def interrupt(self) -> None:
        """
        Stop ALL current and queued speech immediately (e.g. user pressed the
        mic hotkey again). Race-free: bump the generation so anything already
        in flight is orphaned, then drain the queues and reset the parser.
        The worker threads keep running, ready for the next response.
        """
        with self._gen_lock:
            self._gen += 1
        self._drain(self._text_q)
        self._drain(self._audio_q)
        self._buffer.reset()
        log.info("TTS interrupted (generation -> %d).", self._gen)

    # -- buffer callbacks (producer thread) ---------------------------------
    def _on_sentence(self, sentence: str) -> None:
        self._text_q.put((self._current_gen(), sentence))

    def _on_code(self, line_count: int) -> None:
        summary = f"Code block, {line_count} lines, shown on screen."
        self._text_q.put((self._current_gen(), summary))

    # -- synth thread -------------------------------------------------------
    def _synth_loop(self) -> None:
        while not self._shutdown.is_set():
            item = self._text_q.get()
            if item is _SENTINEL:
                break
            gen, sentence = item
            if gen != self._current_gen():
                continue  # orphaned by an interrupt — skip
            try:
                audio = b"".join(
                    chunk.audio_int16_bytes
                    for chunk in self._voice.synthesize(sentence)
                )
            except Exception as exc:
                log.warning("synthesis failed for %r: %s", sentence[:40], exc)
                continue
            if audio and gen == self._current_gen():
                self._audio_q.put((gen, audio))

    # -- playback thread ----------------------------------------------------
    def _play_loop(self) -> None:
        import sounddevice as sd

        block = config.TTS_PLAYBACK_BLOCK
        while not self._shutdown.is_set():
            item = self._audio_q.get()
            if item is _SENTINEL:
                break
            gen, audio = item
            if gen != self._current_gen():
                continue  # orphaned — don't play stale audio

            samples = np.frombuffer(audio, dtype=np.int16)
            with self._active_lock:
                self._active = True
            try:
                with sd.OutputStream(
                    samplerate=self._sample_rate, channels=1, dtype="int16"
                ) as stream:
                    for i in range(0, len(samples), block):
                        # Honour interruption within ~one block (~90ms).
                        if gen != self._current_gen() or self._shutdown.is_set():
                            break
                        stream.write(samples[i:i + block])
            except Exception as exc:
                log.warning("playback failed: %s", exc)
            finally:
                with self._active_lock:
                    self._active = False

    @staticmethod
    def _drain(q: "queue.Queue") -> None:
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass


# ---------------------------------------------------------------------------
# Demo entry point: stream from Ollama and speak it.
# ---------------------------------------------------------------------------
def main() -> int:
    import json
    import sys
    import urllib.request

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say one short friendly sentence."

    engine = TTSEngine()
    engine.start()

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps({
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": True,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                tok = obj.get("response", "")
                if tok:
                    print(tok, end="", flush=True)  # visual stream
                    engine.feed(tok)
                if obj.get("done"):
                    break
        print()
        engine.flush()
        engine._text_q.join() if hasattr(engine._text_q, "join") else None
        import time
        time.sleep(0.5)  # let the tail finish speaking
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
