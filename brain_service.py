#!/usr/bin/env python3
"""
brain_service.py — Phase 7.3: the server-side "brain" behind the WebSocket.

Pulls the LLM + retrieval pieces out of the local orchestrator and makes them
multi-tenant and server-friendly:

  * retrieve_user_context(username, question) — embeds the question and queries
    ONLY that user's `{username}_codebase_vault` (Phase 7.2), returning a prompt
    string with the relevant code chunks.
  * stream_chat(messages) — async generator that streams Ollama /api/chat tokens
    without blocking the event loop (blocking HTTP runs in a worker thread, fed
    back through an asyncio.Queue).
  * transcribe_pcm16(pcm_bytes) — wraps raw 16kHz mono Int16 PCM as a WAV and
    runs the existing faster-whisper engine (voice-in).

LLM/embedding settings mirror tracker/config.py so behaviour matches the local
app. Everything is env-overridable for deployment.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import threading
import urllib.request
import wave
from pathlib import Path

log = logging.getLogger("nexus.brain")

# ─────────────────────────────────────────────────────────────────────────────
# Config (mirrors tracker/config.py; override via env for deployment)
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("NEXUS_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("NEXUS_OLLAMA_MODEL", "qwen2.5-coder:1.5b")
OLLAMA_KEEP_ALIVE = os.getenv("NEXUS_OLLAMA_KEEP_ALIVE", "30m")
# Token caps: short for VOICE (fast generation + short TTS), longer for typed
# chat. Long replies were the main cause of the 30-120s latency on CPU.
OLLAMA_NUM_PREDICT_VOICE = int(os.getenv("NEXUS_NUM_PREDICT_VOICE", "90"))
OLLAMA_NUM_PREDICT_TEXT = int(os.getenv("NEXUS_NUM_PREDICT_TEXT", "256"))
EMBED_MODEL = os.getenv("NEXUS_EMBED_MODEL", "nomic-embed-text")

RAG_TOP_K = int(os.getenv("NEXUS_RAG_TOP_K", "3"))
# Stricter cutoff so casual questions don't drag in unrelated code chunks (the
# cause of the agent rambling about server.py when asked "see my open file?").
RAG_MAX_DISTANCE = float(os.getenv("NEXUS_RAG_MAX_DISTANCE", "0.42"))

# Piper voice model (lessac-medium ships in the project root, 22050 Hz mono).
TTS_MODEL_PATH = os.getenv("NEXUS_TTS_MODEL", "en_US-lessac-medium.onnx")

SYSTEM_PROMPT = (
    "You are Nexus, a friendly AI assistant that can see the user's screen. "
    "Talk like a helpful human colleague — natural, direct, and brief. "
    "When the user asks about their 'current/open file' or what's 'on screen', "
    "answer using the [ACTIVE SCREEN] context (the open file name and contents). "
    "If that context isn't present, simply say you can't see a file open right "
    "now — do NOT guess or describe unrelated code. Never dump large blocks of "
    "code unless explicitly asked. If you don't know, say so in one sentence."
)

# Appended for VOICE turns — answers are spoken aloud, so keep them tiny.
VOICE_CLAUSE = (
    " This is a spoken voice conversation: reply in 1-2 short sentences, "
    "plain words only, no code blocks, no markdown, no lists."
)


# ─────────────────────────────────────────────────────────────────────────────
# Embedding (single query) — local Ollama nomic-embed-text
# ─────────────────────────────────────────────────────────────────────────────
def _embed_query(text: str) -> list:
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": [text]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["embeddings"][0]


# Max chars of the active file we inline into the prompt. Smaller = faster
# prompt-eval on CPU (the main latency lever); the full file is still in the
# vault for retrieval if needed.
ACTIVE_FILE_MAX_CHARS = int(os.getenv("NEXUS_ACTIVE_FILE_MAX_CHARS", "2800"))


async def retrieve_user_context(username: str, question: str, active: dict | None = None) -> str:
    """
    Build the user-content prompt for `question`, grounded in (1) the user's
    CURRENTLY OPEN file (if the client reported one) and (2) semantic hits from
    THIS user's codebase vault. Runs the blocking embed + Chroma query in a
    worker thread. Degrades gracefully (to just the question) on any failure.

    `active` (optional): {window_title, app_name, file_name, file_content} — the
    live screen context forwarded by the client's native "eyes".
    """
    loop = asyncio.get_running_loop()

    # The active-file block is built outside the executor (no I/O).
    active = active or {}
    blocks: list[str] = []
    win = (active.get("window_title") or "").strip()
    app = (active.get("app_name") or "").strip()
    fname = (active.get("file_name") or "").strip()
    fcontent = active.get("file_content") or ""
    # Only treat the file content as "the open file" if its name actually appears
    # in the focused window title — otherwise it's a stale/scanned file, not what
    # the user is looking at, and showing it would mislead the answer.
    content_matches_window = bool(fname) and bool(win) and fname.lower() in win.lower()
    if win or fname:
        header = "[ACTIVE SCREEN] "
        if content_matches_window:
            header += f"currently open file: {fname}"
        if win:
            header += f"  (focused window: {win}{' · ' + app if app else ''})"
        if content_matches_window and fcontent.strip():
            body = fcontent[:ACTIVE_FILE_MAX_CHARS]
            if len(fcontent) > ACTIVE_FILE_MAX_CHARS:
                body += "\n…[truncated]"
            blocks.append(f"{header}\nIts full contents:\n{body}")
        else:
            blocks.append(header)

    def _work() -> str:
        try:
            from memory_manager import get_memory

            qvec = _embed_query(question)
            codebase = get_memory().codebase_vault(username)
            res = codebase.query(
                query_embeddings=[qvec],
                n_results=RAG_TOP_K,
                include=["documents", "metadatas", "distances"],
            )
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]

            kept = []
            for doc, meta, dist in zip(docs, metas, dists):
                if dist is not None and dist > RAG_MAX_DISTANCE:
                    continue  # too far → irrelevant, drop it
                name = (meta or {}).get("file_name", "unknown")
                kept.append(f"# from {name}\n{doc}")
            if kept:
                return "[OTHER RELEVANT CODE FROM YOUR WORKSPACE]\n" + "\n\n".join(kept)
            return ""
        except Exception as exc:  # noqa: BLE001 — retrieval is best-effort
            log.warning("retrieval failed for %r: %s", username, exc)
            return ""

    # Speed: when the open file is already inlined, SKIP the vault RAG. The
    # embed call swaps Ollama from the chat model to nomic-embed-text and back —
    # a big CPU hit — and the open file is usually all the context we need.
    if content_matches_window and fcontent.strip():
        return "\n\n".join(blocks) + f"\n\n[QUESTION]\n{question}"

    rag = await loop.run_in_executor(None, _work)
    if rag:
        blocks.append(rag)

    if not blocks:
        return question
    return "\n\n".join(blocks) + f"\n\n[QUESTION]\n{question}"


# ─────────────────────────────────────────────────────────────────────────────
# Streaming chat — Ollama /api/chat, non-blocking via thread + queue
# ─────────────────────────────────────────────────────────────────────────────
async def stream_chat(messages: list, num_predict: int = OLLAMA_NUM_PREDICT_TEXT):
    """
    Async generator yielding response token strings from Ollama /api/chat.

    The blocking HTTP read runs in a thread; tokens are handed to the event loop
    through an asyncio.Queue. On error, yields a single ("__error__", message)
    tuple so the caller can surface it. Always terminates.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def _worker() -> None:
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=json.dumps(
                    {
                        "model": OLLAMA_MODEL,
                        "messages": messages,
                        "stream": True,
                        "keep_alive": OLLAMA_KEEP_ALIVE,
                        "options": {"num_predict": num_predict},
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    raw = raw.strip()
                    if not raw:
                        continue
                    obj = json.loads(raw)
                    token = (obj.get("message") or {}).get("content", "")
                    if token:
                        loop.call_soon_threadsafe(queue.put_nowait, token)
                    if obj.get("done"):
                        break
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("__error__", str(exc)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

    loop.run_in_executor(None, _worker)

    while True:
        item = await queue.get()
        if item is SENTINEL:
            return
        yield item


def build_messages(history: list, user_content: str, voice: bool = False) -> list:
    """Assemble the Ollama messages array: system + rolling history + this turn."""
    system = SYSTEM_PROMPT + (VOICE_CLAUSE if voice else "")
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history)  # [{"role": "user"|"assistant", "content": ...}, ...]
    msgs.append({"role": "user", "content": user_content})
    return msgs


# ─────────────────────────────────────────────────────────────────────────────
# Voice-in: raw PCM → WAV → faster-whisper transcript
# ─────────────────────────────────────────────────────────────────────────────
def _pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> io.BytesIO:
    """Wrap raw mono Int16 PCM as an in-memory 16kHz WAV (what STT expects)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    buf.seek(0)
    return buf


async def transcribe_pcm16(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """
    Transcribe accumulated mic PCM (the Phase 6.2 client stream). Runs the
    blocking faster-whisper call in a worker thread. Returns '' on empty/failure.
    """
    if not pcm_bytes:
        return ""
    loop = asyncio.get_running_loop()

    def _work() -> str:
        try:
            from tracker.stt_engine import transcribe_audio

            wav = _pcm16_to_wav(pcm_bytes, sample_rate)
            return transcribe_audio(wav) or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("STT failed: %s", exc)
            return ""

    return await loop.run_in_executor(None, _work)


# ─────────────────────────────────────────────────────────────────────────────
# Voice-out: text → Piper → Int16 PCM (base64) for the client to play
# ─────────────────────────────────────────────────────────────────────────────
_piper_voice = None
_piper_lock = threading.Lock()


def _get_piper():
    """Lazily load the one process-wide Piper voice (heavy; load once)."""
    global _piper_voice
    with _piper_lock:
        if _piper_voice is None:
            from piper import PiperVoice

            log.info("loading Piper voice: %s", TTS_MODEL_PATH)
            _piper_voice = PiperVoice.load(str(Path(TTS_MODEL_PATH)))
        return _piper_voice


async def synthesize_tts(text: str):
    """
    Synthesize `text` to speech with Piper and return (pcm_b64, sample_rate) —
    base64-encoded mono Int16 PCM the client plays via Web Audio. Returns None on
    empty text or failure. Markdown is stripped so code blocks aren't read aloud.
    Runs the blocking synth in a worker thread.
    """
    if not text or not text.strip():
        return None
    loop = asyncio.get_running_loop()

    def _work():
        try:
            # Reuse the local app's markdown cleaner so symbols/code aren't voiced.
            try:
                from tracker.tts_engine import clean_markdown

                spoken = clean_markdown(text)
            except Exception:  # noqa: BLE001 — fall back to raw text
                spoken = text
            if not spoken.strip():
                return None

            voice = _get_piper()
            pcm = bytearray()
            sample_rate = 22050
            for chunk in voice.synthesize(spoken):
                pcm.extend(chunk.audio_int16_bytes)
                sample_rate = chunk.sample_rate
            if not pcm:
                return None
            return base64.b64encode(bytes(pcm)).decode("ascii"), sample_rate
        except Exception as exc:  # noqa: BLE001 — TTS is best-effort
            log.warning("TTS synthesis failed: %s", exc)
            return None

    return await loop.run_in_executor(None, _work)
