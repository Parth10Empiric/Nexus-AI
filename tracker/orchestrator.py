"""
orchestrator.py — Phase 4.4 "The Conversational Orchestrator".

The central controller that wires the voice pipeline into one seamless,
interruptible loop — "Nexus Ten":

    push-to-talk ─▶ record (4.1) ─▶ transcribe (4.2) ─▶ mix live code context
    (3.3) ─▶ stream Ollama (4) ─▶ speak with Piper (4.3) ─▶ remember the turn.

It is an event-driven asyncio state machine. The hotkey events arrive from
pynput's listener thread and are marshalled onto the asyncio loop; the heavy,
blocking work (Whisper, Ollama HTTP) runs in executor threads so the loop stays
responsive enough to handle a barge-in at any instant.

States:  IDLE → LISTENING → THINKING → SPEAKING → IDLE
Barge-in: pressing the hotkey during THINKING/SPEAKING instantly cancels the
turn, silences the audio, and returns to LISTENING for the new question.

Run it:   python -m tracker.orchestrator
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.request
from collections import deque
from enum import Enum, auto
from typing import Optional

from . import config
from .audio_capture import AudioRecorder, PushToTalkController
from .context_mixer import is_self_referential, load_active_context
from .stt_engine import transcribe_audio
from .tts_engine import TTSEngine

log = logging.getLogger("nexus.orchestrator")

PERSONA = (
    "Your name is Nexus Ten. You are an elite, highly conversational software "
    "engineer assisting me with my code. Speak naturally, confidently, and "
    "concisely like a human. Do not use markdown, emojis, or robotic formatting "
    "in your spoken responses."
)


class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


class Orchestrator:
    def __init__(self) -> None:
        self.state = State.IDLE
        self.history: deque = deque(maxlen=config.CONV_HISTORY_TURNS)

        self.tts = TTSEngine()
        self.recorder = AudioRecorder()
        self.ptt = PushToTalkController(
            on_audio=self._on_audio_threadsafe,
            on_record_start=self._on_press_threadsafe,
            recorder=self.recorder,
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._events: Optional[asyncio.Queue] = None
        self._active_task: Optional[asyncio.Task] = None
        self._abort = threading.Event()  # the CURRENT turn's abort flag
        self._shutdown = False

    # ----- thread → asyncio-loop bridges -----------------------------------
    def _on_press_threadsafe(self) -> None:
        """Called on pynput's thread the moment a new recording starts."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._post, ("press",))

    def _on_audio_threadsafe(self, wav) -> None:
        """Called on pynput's thread when the hotkey is released (audio ready)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._post, ("audio", wav))

    def _post(self, event) -> None:
        if self._events is not None:
            self._events.put_nowait(event)

    # ----- main loop -------------------------------------------------------
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._events = asyncio.Queue()
        self.tts.start()
        self.ptt.start()
        log.info("🟢 Nexus Ten ready. Hold %s+%s to talk (Ctrl+C to quit).",
                 config.PTT_MODIFIER.upper(), config.PTT_KEY.upper())
        self._set_state(State.IDLE)
        try:
            while not self._shutdown:
                event = await self._events.get()
                await self._dispatch(event)
        finally:
            await self._barge_in()       # cancel anything in flight
            self.ptt.stop()
            self.tts.stop()
            log.info("Nexus Ten stopped.")

    async def _dispatch(self, event) -> None:
        kind = event[0]
        if kind == "press":
            # Hotkey pressed. If we're mid-answer, this is a BARGE-IN.
            if self.state in (State.THINKING, State.SPEAKING):
                await self._barge_in()
            self._set_state(State.LISTENING)
        elif kind == "audio":
            # Hotkey released — process the captured question as a new turn.
            wav = event[1]
            self._active_task = asyncio.create_task(self._handle_turn(wav))

    # ----- barge-in --------------------------------------------------------
    async def _barge_in(self) -> None:
        """Instantly stop speaking/thinking and discard the in-flight turn."""
        self._abort.set()        # signal the streaming executor thread to stop
        self.tts.interrupt()     # silence audio + drain TTS queues immediately
        task, self._active_task = self._active_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        log.info("⛔ barge-in: interrupted, ready to listen.")

    # ----- a full conversational turn --------------------------------------
    async def _handle_turn(self, wav) -> None:
        loop = asyncio.get_running_loop()
        abort = threading.Event()      # this turn's own abort flag
        self._abort = abort
        try:
            self._set_state(State.THINKING)

            # 1. Transcribe (CPU-heavy → executor so the loop stays responsive).
            question = await loop.run_in_executor(None, transcribe_audio, wav)
            question = (question or "").strip()
            if not question or abort.is_set():
                log.info("(no speech detected)")
                self._set_state(State.IDLE)
                return
            log.info("🗣️  USER: %s", question)

            # 2. Build the system prompt (persona + live screen/code context).
            system = self._build_system_prompt()
            messages = self._build_messages(system, question)

            # 3. Stream Ollama → Piper. Speaking starts on the first sentence.
            self._set_state(State.SPEAKING)
            answer = await loop.run_in_executor(
                None, self._stream_to_tts, messages, abort
            )
            if abort.is_set():
                return
            self.tts.flush()

            # 4. Wait for speech to finish (cancellable / interruptible).
            while self.tts.is_speaking and not abort.is_set():
                await asyncio.sleep(0.1)

            # 5. Remember the turn (only if it completed).
            if answer and not abort.is_set():
                self.history.append({"user": question, "assistant": answer})
                log.info("🤖 NEXUS TEN: %s", answer.strip())

            if not abort.is_set():
                self._set_state(State.IDLE)
        except asyncio.CancelledError:
            raise  # barge-in cancelled us; state already moved to LISTENING
        except Exception as exc:
            log.error("turn failed: %s", exc)
            self._set_state(State.IDLE)

    # ----- Ollama streaming (runs in an executor thread) -------------------
    def _stream_to_tts(self, messages, abort: threading.Event) -> str:
        """Blocking: stream /api/chat, feed tokens to TTS, honour `abort`."""
        req = urllib.request.Request(
            f"{config.OLLAMA_URL}/api/chat",
            data=json.dumps({
                "model": config.OLLAMA_MODEL,
                "messages": messages,
                "stream": True,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for line in resp:
                    if abort.is_set():
                        break  # barge-in: stop reading, close the connection
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    token = obj.get("message", {}).get("content", "")
                    if token:
                        parts.append(token)
                        self.tts.feed(token)
                    if obj.get("done"):
                        break
        except Exception as exc:
            log.error("Ollama stream failed: %s", exc)
        return "".join(parts)

    # ----- prompt construction --------------------------------------------
    def _build_system_prompt(self) -> str:
        """Persona + the developer's current on-screen file (self-excluded)."""
        ctx = load_active_context()
        if ctx and ctx.file_content.strip() and not is_self_referential(ctx):
            code = ctx.file_content[: config.MAX_CONTEXT_CHARS]
            return (
                PERSONA + "\n\n"
                f"The developer is currently working in the file '{ctx.file_name}' "
                f"(path: {ctx.absolute_path}). Here is its current content:\n\n"
                f"{code}\n\n"
                "When the developer asks about 'this code', 'this file', 'this "
                "function', or what is on their screen, answer using the file "
                "above. Explain it in plain spoken English — never read code "
                "character by character."
            )
        return PERSONA

    def _build_messages(self, system: str, question: str) -> list[dict]:
        messages = [{"role": "system", "content": system}]
        for turn in self.history:          # short-term memory
            messages.append({"role": "user", "content": turn["user"]})
            messages.append({"role": "assistant", "content": turn["assistant"]})
        messages.append({"role": "user", "content": question})
        return messages

    # ----- helpers ---------------------------------------------------------
    def _set_state(self, state: State) -> None:
        self.state = state
        log.info("[STATE] %s", state.name)

    def request_shutdown(self) -> None:
        self._shutdown = True
        self._post(("noop",))  # unblock the queue


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    orch = Orchestrator()
    try:
        await orch.run()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
