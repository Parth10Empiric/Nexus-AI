"""
wake_orchestrator.py — Phase 4.5 "UI Orchestrator & Wake-Word Context Engine".

The always-watching variant of Nexus Ten. The React "Voice Agent Mode" toggle
arms it; from then on it listens hands-free for the wake word, and on each
utterance it assembles the OMNISCIENT context (live screen code + recent
history) and answers as the human-like "Nexus Ten" — emitting its state
(standby/listening/thinking/speaking) back to the UI the whole time.

    UI toggle ON ─▶ wake listener armed (low CPU)
        │  "hey jarvis" detected ─▶ emit LISTENING, capture utterance
        ▼
    transcribe (Whisper) ─▶ assemble omniscient context ─▶ emit THINKING
        ▼
    master prompt ─▶ stream Ollama ─▶ Piper speaks ─▶ emit SPEAKING ─▶ standby

Heavy work runs in executor threads; the asyncio loop stays free to handle the
next wake event or a UI toggle-off at any time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.request
from collections import deque
from typing import Optional

from . import config, context_engine
from .stt_engine import transcribe_audio
from .tts_engine import TTSEngine
from .ui_bridge import UIBridge
from .wake_word import WakeWordListener

log = logging.getLogger("nexus.wake_orchestrator")


class WakeOrchestrator:
    def __init__(self) -> None:
        self.tts = TTSEngine()
        self.bridge = UIBridge(on_command=self._on_ui_command)
        self.wake = WakeWordListener(
            on_wake=self._on_wake_threadsafe,
            on_utterance=self._on_utterance_threadsafe,
        )
        self.history: deque = deque(maxlen=config.CONV_HISTORY_TURNS)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._armed = False
        self._busy = False           # a turn is being processed
        self._abort = threading.Event()
        self._shutdown = asyncio.Event()

    # ----- UI commands -----------------------------------------------------
    async def _on_ui_command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "activate":
            await self._arm()
        elif cmd == "deactivate":
            await self._disarm()

    async def _arm(self) -> None:
        if self._armed:
            return
        self._armed = True
        # Loading + opening the mic can block briefly → executor.
        await asyncio.get_running_loop().run_in_executor(None, self.wake.start)
        await self.bridge.emit_state("standby", "Say the wake word")
        log.info("Voice Agent Mode: ON")

    async def _disarm(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self._abort.set()
        self.tts.interrupt()
        await asyncio.get_running_loop().run_in_executor(None, self.wake.stop)
        await self.bridge.emit_state("off")
        log.info("Voice Agent Mode: OFF")

    # ----- wake/utterance bridges (audio thread → loop) --------------------
    def _on_wake_threadsafe(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.bridge.emit_state("listening"))
            )

    def _on_utterance_threadsafe(self, wav) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._handle_utterance(wav))
            )

    # ----- a full turn -----------------------------------------------------
    async def _handle_utterance(self, wav) -> None:
        if self._busy or not self._armed:
            return
        self._busy = True
        abort = threading.Event()
        self._abort = abort
        loop = asyncio.get_running_loop()
        try:
            await self.bridge.emit_state("thinking", "Transcribing")
            question = (await loop.run_in_executor(None, transcribe_audio, wav) or "").strip()
            if not question or abort.is_set():
                await self.bridge.emit_state("standby", "Say the wake word")
                return
            log.info("🗣️  USER: %s", question)

            # OMNISCIENT context: live screen + recent history (read-only DB).
            ctx = await loop.run_in_executor(None, context_engine.assemble_context)
            prompt = context_engine.build_master_prompt(question, ctx)
            messages = self._build_messages(prompt)

            await self.bridge.emit_state("speaking", "Nexus Ten")
            answer = await loop.run_in_executor(None, self._stream_to_tts, messages, abort)
            if abort.is_set():
                return
            self.tts.flush()
            while self.tts.is_speaking and not abort.is_set():
                await asyncio.sleep(0.1)

            if answer and not abort.is_set():
                self.history.append({"user": question, "assistant": answer})
                log.info("🤖 NEXUS TEN: %s", answer.strip())
        except Exception as exc:
            log.error("turn failed: %s", exc)
        finally:
            self._busy = False
            if self._armed and not abort.is_set():
                await self.bridge.emit_state("standby", "Say the wake word")

    # ----- Ollama streaming (executor thread) ------------------------------
    def _stream_to_tts(self, messages, abort: threading.Event) -> str:
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
                        break
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

    def _build_messages(self, master_prompt: str) -> list[dict]:
        # The master prompt already carries persona + screen + history + the
        # spoken question, so it is the single user turn. We still replay prior
        # spoken turns for conversational memory.
        messages = []
        for turn in self.history:
            messages.append({"role": "user", "content": turn["user"]})
            messages.append({"role": "assistant", "content": turn["assistant"]})
        messages.append({"role": "user", "content": master_prompt})
        return messages

    # ----- lifecycle -------------------------------------------------------
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.tts.start()
        await self.bridge.start()
        await self.bridge.emit_state("off")
        log.info("🟢 Nexus Ten UI orchestrator running. Toggle Voice Agent Mode in the app.")
        try:
            await self._shutdown.wait()
        finally:
            await self._disarm()
            self.tts.stop()
            await self.bridge.stop()
            log.info("UI orchestrator stopped.")

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
        asyncio.run(WakeOrchestrator().run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
