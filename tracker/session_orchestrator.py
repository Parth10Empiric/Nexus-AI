"""
session_orchestrator.py — Phase 4.5 (refactor): Continuous Multi-Turn Voice
Session with Barge-in.

Supersedes the single-turn orchestrators. A four-state asyncio machine drives
one always-open mic (via VoiceFrontend):

    STANDBY        — passive openwakeword, <2% CPU. Wake word → ACTIVE_LISTENING.
    ACTIVE_LISTENING — VAD endpointing captures one utterance.
                       • "bye/goodbye/sleep nexus" → clear memory, beep, STANDBY.
                       • silence timeout → STANDBY.
                       • otherwise → THINKING.
    THINKING       — transcribe + omniscient context + Ollama.
    SPEAKING       — Piper streams the answer; the mic stays open in GUARD mode.
                       • sustained user voice (barge-in) → kill TTS, → ACTIVE_LISTENING.
                       • natural finish → back to ACTIVE_LISTENING (NO wake word).

A rolling memory buffer (last N messages) lives for the whole session so Nexus
remembers the conversation. The UI bridge emits every state so React mirrors
"Sleeping / Listening / Thinking / Speaking".
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

import numpy as np

from . import config, context_engine, retriever
from .memory_manager import get_memory
from .stt_engine import transcribe_audio
from .tts_engine import TTSEngine
from .ui_bridge import UIBridge
from .voice_frontend import (MODE_GUARD, MODE_LISTEN, MODE_OFF, MODE_STANDBY,
                             VoiceFrontend)

log = logging.getLogger("nexus.session")


class State(Enum):
    STANDBY = auto()
    ACTIVE_LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


# UI-facing names for each state.
_UI = {
    State.STANDBY: "sleeping",
    State.ACTIVE_LISTENING: "listening",
    State.THINKING: "thinking",
    State.SPEAKING: "speaking",
}


class SessionOrchestrator:
    def __init__(self) -> None:
        self.state = State.STANDBY
        self.memory: deque = deque(maxlen=config.SESSION_MEMORY_MESSAGES)

        self.tts = TTSEngine()
        self.vault = get_memory()            # shared ChromaDB singleton (Phase 5.2)
        try:
            from .vector_indexer import VectorIndexer
            self.indexer = VectorIndexer(memory=self.vault)
        except Exception:
            self.indexer = None
        self.bridge = UIBridge(on_command=self._on_ui_command)
        self.voice = VoiceFrontend(
            on_wake=self._cb_wake,
            on_utterance=self._cb_utterance,
            on_bargein=self._cb_bargein,
        )

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._abort = threading.Event()
        self._turn_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()
        self._armed = False

        # Shift+I global interrupt hotkey (pynput listener thread).
        self._hotkey = None
        self._shift_down = False

    # ===== UI ==============================================================
    async def _on_ui_command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "ask":
            # Desktop TEXT chat: a typed question, answered on screen + files
            # via the SAME pipeline as voice (and spoken aloud too).
            text = (msg.get("text") or "").strip()
            if text:
                asyncio.create_task(self._handle_text_turn(text))
            return
        if cmd == "interrupt":           # UI "Interrupt" button (= Shift+I)
            await self._interrupt()
            return
        if cmd == "activate" and not self._armed:
            self._armed = True
            await asyncio.get_running_loop().run_in_executor(None, self.voice.start)
            self._start_hotkey()
            await self._enter_standby()
        elif cmd == "deactivate" and self._armed:
            await self._deactivate_agent(beep=False)

    # ===== Shift+I global interrupt hotkey =================================
    def _start_hotkey(self) -> None:
        if self._hotkey is not None:
            return
        try:
            from pynput import keyboard
        except Exception as exc:
            log.warning("interrupt hotkey unavailable: %s", exc)
            return

        shift_keys = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}

        def on_press(key):
            if key in shift_keys:
                self._shift_down = True
                return
            ch = getattr(key, "char", None)
            if self._shift_down and ch and ch.lower() == "i":
                if self._loop:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self._interrupt()))

        def on_release(key):
            if key in shift_keys:
                self._shift_down = False

        self._hotkey = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._hotkey.daemon = True
        self._hotkey.start()
        log.info("interrupt hotkey armed: Shift+I")

    def _stop_hotkey(self) -> None:
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
            self._hotkey = None

    async def _set_state(self, state: State, ui: Optional[str] = None) -> None:
        self.state = state
        await self.bridge.emit_state(ui or _UI[state])
        log.info("[STATE] %s", state.name)

    # ===== frontend callbacks (audio thread → loop) ========================
    def _schedule(self, coro) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(coro))

    def _cb_wake(self) -> None:
        self._schedule(self._on_wake())

    def _cb_utterance(self, wav) -> None:
        self._schedule(self._on_utterance(wav))

    def _cb_bargein(self) -> None:
        self._schedule(self._on_bargein())

    # ===== state entries ===================================================
    async def _enter_standby(self) -> None:
        await self._set_state(State.STANDBY)
        self.voice.set_mode(MODE_STANDBY)

    async def _enter_listening(self) -> None:
        await self._set_state(State.ACTIVE_LISTENING)
        self.voice.set_mode(MODE_LISTEN)
        # Clear terminal feedback that Nexus is ready for the next question.
        print("\n🎧 Listening...  (ask your next question — say 'bye' to turn off)")

    def _rearm_listen(self) -> None:
        """Re-open the VAD without re-printing — used after a silent timeout so
        the session keeps waiting instead of falling asleep."""
        self.voice.set_mode(MODE_LISTEN)

    # ===== events ==========================================================
    async def _on_wake(self) -> None:
        if not self._armed or self.state != State.STANDBY:
            return
        log.info("🔔 wake word — session started.")
        print("\n🔔 Wake word detected — Nexus is awake!")
        await self._enter_listening()

    async def _on_utterance(self, wav) -> None:
        # Only meaningful while armed AND actively listening (ignore anything
        # that slips in after the agent was turned off).
        if not self._armed or self.state != State.ACTIVE_LISTENING:
            return
        if wav is None:
            # Silence so far. Stay awake and keep listening (loop continues
            # until an explicit "bye jarvis"); only sleep if configured off.
            if config.SESSION_STAY_AWAKE:
                self._rearm_listen()
            else:
                log.info("(idle timeout — going back to sleep)")
                await self._end_session(beep=False)
            return
        self._turn_task = asyncio.create_task(self._handle_turn(wav))

    async def _on_bargein(self) -> None:
        # Voice barge-in is disabled (half-duplex); kept for compatibility.
        await self._interrupt()

    async def _interrupt(self) -> None:
        """Stop the current answer (Shift+I or UI) and re-open the mic to listen."""
        if self.state not in (State.THINKING, State.SPEAKING):
            return
        log.info("✋ interrupt — stopping reply, listening again.")
        print("\n✋ Interrupted — listening...")
        await self._cancel_turn()
        await self._enter_listening()

    # ===== a turn ==========================================================
    async def _handle_turn(self, wav) -> None:
        loop = asyncio.get_running_loop()
        abort = threading.Event()
        self._abort = abort
        try:
            await self._set_state(State.THINKING)
            question = (await loop.run_in_executor(None, transcribe_audio, wav) or "").strip()
            if abort.is_set() or not self._armed:
                return                        # turned off / interrupted mid-turn
            if not question:
                await self._enter_listening()
                return
            log.info("🗣️  USER: %s", question)
            print(f"🗣️  You: {question}")

            # 1. Goodbye ("bye", "bye jarvis", "goodbye", …) → STOP + turn the
            #    agent OFF (UI toggle flips off). Not just sleep.
            if self._is_goodbye(question):
                log.info("👋 goodbye heard — turning the agent off.")
                print("👋 Goodbye — agent off. Toggle Voice Agent Mode on to restart.")
                await self._deactivate_agent(beep=True)
                return

            # 2. Pure interrupt/cancel word ("stop", "okay", …) → don't answer,
            #    just go back to listening for the real next question.
            normalized = question.lower().strip(" .,!?")
            if normalized in config.SESSION_INTERRUPT_WORDS:
                log.info("✋ interrupt word ('%s') — ready for next question.", normalized)
                await self._enter_listening()
                return

            # Phase 5.3: hybrid retrieval — live screen + global codebase + work
            # history, fetched in parallel and relevance-filtered.
            user_content = await self._gather_context(question)
            messages = self._build_messages(user_content)

            await self._set_state(State.SPEAKING)
            # Half-duplex: the mic stays OFF while thinking/speaking so Nexus
            # never hears itself. Interrupt with Shift+I (or the UI) instead.
            self.voice.set_mode(MODE_OFF)
            answer = await loop.run_in_executor(None, self._stream_to_tts, messages, abort)
            if abort.is_set():
                return
            self.tts.flush()
            while self.tts.is_speaking and not abort.is_set():
                await asyncio.sleep(0.1)

            if abort.is_set():
                return
            # Remember the exchange (rolling buffer of messages).
            self.memory.append({"role": "user", "content": question})
            if answer:
                self.memory.append({"role": "assistant", "content": answer})
                log.info("🤖 NEXUS: %s", answer.strip())
                print(f"🤖 Nexus: {answer.strip()}")

            # Multi-turn: go straight back to listening — no wake word needed.
            await self._enter_listening()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("turn failed: %s", exc)
            if self._armed:
                await self._enter_listening()

    async def _gather_context(self, question: str) -> str:
        """Phase 5.3 hybrid retrieval → the master-prompt user content. Never
        raises: a failure here still yields a usable (smaller) prompt."""
        loop = asyncio.get_running_loop()
        # Embed the currently-open file first (deduped — free if unchanged), so
        # the question can also match it via global retrieval.
        if self.indexer is not None:
            try:
                await loop.run_in_executor(None, self.indexer.index_active_file)
            except Exception as exc:
                log.debug("active-file index skipped: %s", exc)
        try:
            return await retriever.retrieve_and_build(
                question, self.vault, self._format_memory())
        except Exception as exc:
            log.warning("context retrieval failed: %s", exc)
            return f"[USER SPOKE]: {question}"

    async def _handle_text_turn(self, text: str) -> None:
        """Handle a typed question from the desktop chat: same screen+files
        context as voice, stream the answer to the UI, and speak it too."""
        loop = asyncio.get_running_loop()
        abort = threading.Event()
        self._abort = abort
        log.info("⌨️  TEXT: %s", text)
        await self.bridge.emit({"type": "user", "text": text})
        try:
            user_content = await self._gather_context(text)
            messages = self._build_messages(user_content)
            await self.bridge.emit_state("thinking", "Typing answer")

            def on_token(tok: str) -> None:
                self.bridge.emit_threadsafe({"type": "token", "token": tok})

            answer = await loop.run_in_executor(
                None, self._stream_to_tts, messages, abort, on_token)
            self.tts.flush()
            if answer:
                self.memory.append({"role": "user", "content": text})
                self.memory.append({"role": "assistant", "content": answer})
            await self.bridge.emit({"type": "answer", "text": answer})
        except Exception as exc:
            log.error("text turn failed: %s", exc)
            await self.bridge.emit({"type": "answer", "text": f"(error: {exc})"})
        finally:
            await self.bridge.emit_state(_UI.get(self.state, "sleeping"))

    async def _cancel_turn(self) -> None:
        self._abort.set()
        self.tts.interrupt()
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _is_goodbye(text: str) -> bool:
        """True if the utterance is a goodbye that should end the session."""
        low = text.lower()
        # Stop PHRASES ("bye jarvis", "goodbye nexus", …) anywhere in the utterance.
        if any(p in low for p in config.STOP_PHRASES):
            return True
        # A standalone STOP WORD as the whole utterance (or its start),
        # e.g. just "bye" / "goodbye" / "ok bye".
        norm = low.strip(" .,!?")
        words = config.STOP_WORDS
        if norm in words:
            return True
        return any(norm.startswith(w + " ") or norm == w for w in words)

    async def _deactivate_agent(self, beep: bool = False) -> None:
        """Fully stop the agent: cancel any reply, clear memory, stop the wake
        listener + hotkey, and flip the UI toggle OFF."""
        self._armed = False          # set FIRST so in-flight callbacks bail out
        self.voice.set_mode(MODE_OFF)  # stop reacting to audio immediately
        await self._cancel_turn()
        self.memory.clear()
        if beep:
            await asyncio.get_running_loop().run_in_executor(None, self._play_beep)
        self._armed = False
        self._stop_hotkey()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.voice.stop)
        except Exception as exc:
            log.debug("voice stop: %s", exc)
        await self._set_state(State.STANDBY, ui="off")   # UI button → off

    async def _end_session(self, beep: bool) -> None:
        # Kept for the (rare) idle path: clear + go to STANDBY (still armed).
        await self._cancel_turn()
        self.memory.clear()
        if beep:
            await asyncio.get_running_loop().run_in_executor(None, self._play_beep)
        await self._enter_standby()

    async def _teardown_session(self, to_standby: bool) -> None:
        await self._cancel_turn()
        self.voice.set_mode(MODE_OFF)
        if to_standby:
            await self._enter_standby()

    # ===== Ollama (executor thread) ========================================
    def _stream_to_tts(self, messages, abort: threading.Event, on_token=None) -> str:
        req = urllib.request.Request(
            f"{config.OLLAMA_URL}/api/chat",
            data=json.dumps({
                "model": config.OLLAMA_MODEL,
                "messages": messages,
                "stream": True,
                "keep_alive": config.OLLAMA_KEEP_ALIVE,   # keep model resident
                "options": {"num_predict": config.OLLAMA_NUM_PREDICT},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for line in resp:
                    if abort.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    token = obj.get("message", {}).get("content", "")
                    if token:
                        parts.append(token)
                        self.tts.feed(token)
                        if on_token is not None:
                            on_token(token)         # also stream to the UI (text chat)
                    if obj.get("done"):
                        break
        except Exception as exc:
            log.error("Ollama stream failed: %s", exc)
        return "".join(parts)

    # ===== helpers =========================================================
    def _format_memory(self) -> str:
        lines = []
        for m in self.memory:
            who = "User" if m["role"] == "user" else "Nexus"
            lines.append(f"{who}: {m['content']}")
        return "\n".join(lines)

    def _build_messages(self, user_content: str) -> list[dict]:
        # Persona + situational-awareness rules go in the SYSTEM role (small
        # models weight system instructions most heavily — this is what cures
        # context bias). The live screen + question go in the USER role.
        return [
            {"role": "system", "content": context_engine.NEXUS_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _play_beep(self) -> None:
        """Short two-tone confirmation chime via sounddevice (no temp file)."""
        try:
            import sounddevice as sd
            sr = 22050
            t = np.linspace(0, 0.12, int(sr * 0.12), endpoint=False)
            tone = lambda f: (0.25 * np.sin(2 * np.pi * f * t)).astype(np.float32)
            chime = np.concatenate([tone(660), tone(440)])
            sd.play(chime, sr); sd.wait()
        except Exception as exc:
            log.debug("beep failed: %s", exc)

    # ===== lifecycle =======================================================
    async def _warmup(self) -> None:
        """Pre-load every model so the FIRST question isn't slowed by cold
        loads. Whisper + Piper + the LLM are warmed concurrently in executors."""
        loop = asyncio.get_running_loop()
        print("⏳ Warming up models (Whisper, Piper, LLM)…")

        def warm_whisper():
            try:
                from .stt_engine import get_transcriber
                get_transcriber().warmup()
            except Exception as exc:
                log.debug("whisper warmup: %s", exc)

        def warm_llm():
            try:
                req = urllib.request.Request(
                    f"{config.OLLAMA_URL}/api/chat",
                    data=json.dumps({
                        "model": config.OLLAMA_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "keep_alive": config.OLLAMA_KEEP_ALIVE,   # load + pin in RAM
                        "options": {"num_predict": 1},
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=120).read()
            except Exception as exc:
                log.debug("llm warmup: %s", exc)

        # TTS (Piper) is loaded by tts.start(); warm Whisper + LLM in parallel.
        await asyncio.gather(
            loop.run_in_executor(None, warm_whisper),
            loop.run_in_executor(None, warm_llm),
        )
        print("✅ Models ready.")

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.tts.start()
        await self.bridge.start()
        await self._warmup()
        await self.bridge.emit_state("off")
        log.info("🟢 Nexus session orchestrator running.")

        # Auto-arm so it listens for the wake word immediately — no UI toggle
        # required (the toggle still works to disarm/re-arm).
        if config.SESSION_AUTOSTART:
            self._armed = True
            await asyncio.get_running_loop().run_in_executor(None, self.voice.start)
            self._start_hotkey()
            await self._enter_standby()
            print(f"\n💤 Standby — say the wake word ('{config.WAKE_WORD_MODEL.replace('_',' ')}') to start.")
            print("   (press Shift+I to interrupt a reply; say 'bye jarvis' to sleep)")
        else:
            print("Toggle Voice Agent Mode in the dashboard to begin.")

        try:
            await self._shutdown.wait()
        finally:
            if self._armed:
                await self.bridge_safe_stop()
            self.tts.stop()
            await self.bridge.stop()

    async def bridge_safe_stop(self) -> None:
        await self._teardown_session(to_standby=False)
        await asyncio.get_running_loop().run_in_executor(None, self.voice.stop)

    def request_shutdown(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._shutdown.set)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(SessionOrchestrator().run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
