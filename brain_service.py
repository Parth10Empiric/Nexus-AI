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
import random
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
    "Talk like a helpful human colleague — natural, direct, and brief.\n"
    # --- Situational-awareness rules (cure the RLHF disclaimer hallucination) ---
    "CRITICAL — you DO have real-time access to the user's screen and files via "
    "the [ACTIVE SCREEN] context provided in the prompt. Treat that context as "
    "your own direct vision: it is what you are looking at right now. NEVER claim "
    "you cannot see their screen, files, or what they are working on.\n"
    "NEVER say 'As an AI language model', 'As an AI', 'I am a language model', "
    "'I'm just a program', \"I don't have real-time access\", or any similar "
    "disclaimer. You ARE Nexus and you CAN see the screen — act like it.\n"
    # --- Strict current-file adherence (anti-hallucination) --------------------
    "You must identify the 'current file' or 'current window' ONLY from the "
    "[ACTIVE SCREEN CONTEXT] in this prompt — specifically the 'CURRENTLY ACTIVE "
    "WINDOW' and 'CURRENTLY ACTIVE FILE' lines. NEVER decide what is on screen "
    "from earlier conversation history; files mentioned before are NOT on screen "
    "anymore.\n"
    "When 'CURRENTLY ACTIVE FILE' names a file followed by its contents, you CAN "
    "see that file — if the user says 'this file' or 'the file on my screen', "
    "they mean THAT file, so explain it directly from those exact contents and do "
    "NOT ask them to open it. Anything under [BACKGROUND REFERENCE] is older "
    "workspace code, NOT what's on screen — use it only as supporting detail, "
    "never as the current/active file.\n"
    # --- Conversational style (spoken, human, professional) --------------------
    "STYLE — talk like one person having a real conversation with another:\n"
    "- Keep answers to 1-3 short sentences unless the user explicitly asks for "
    "more. NEVER give numbered steps, 'how to' lists, or code blocks — just say "
    "the answer in plain words.\n"
    "- Reply in plain, natural spoken language with warm human expression, like "
    "a knowledgeable colleague guiding a friend. Be professional but friendly.\n"
    "- Output TALK ONLY: no code blocks, no markdown, no bullet lists, no symbols, "
    "no file dumps, no headings — only words a person could say out loud.\n"
    "- Do NOT repeat, quote, or restate the user's question back to them. Jump "
    "straight into the answer as if continuing a conversation.\n"
    "- Never open with filler like 'Sure, here is…' or 'Your question is…'. Just "
    "answer directly and naturally.\n"
    "- If you must mention code, describe what it does in everyday words instead "
    "of showing it."
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
    # ALWAYS emit the active-screen block — even when empty — so the model decides
    # "what's on screen" ONLY from this real-time capture, never from chat history.
    header = "[ACTIVE SCREEN CONTEXT]\n"
    if win:
        header += f"CURRENTLY ACTIVE WINDOW: {win}{' (' + app + ')' if app else ''}\n"
    else:
        header += "CURRENTLY ACTIVE WINDOW: (none reported)\n"
    if content_matches_window and fcontent.strip():
        body = fcontent[:ACTIVE_FILE_MAX_CHARS]
        if len(fcontent) > ACTIVE_FILE_MAX_CHARS:
            body += "\n…[truncated]"
        header += f"CURRENTLY ACTIVE FILE: {fname}\nIts exact contents are:\n{body}"
    else:
        # Nothing is actually visible — state it plainly so the model uses the
        # "Please open that file…" fallback instead of guessing from old context.
        header += "CURRENTLY ACTIVE FILE: (no file contents are visible on screen right now)"
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
                # Clearly demoted: this is BACKGROUND reference material, NOT what
                # is on screen now. The system prompt forbids treating it as the
                # current file, so an unrelated RAG hit can't masquerade as "open".
                return (
                    "[BACKGROUND REFERENCE — older code from the workspace vault. "
                    "This is NOT necessarily on screen right now; do NOT treat it "
                    "as the current/active file.]\n" + "\n\n".join(kept)
                )
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


# ─────────────────────────────────────────────────────────────────────────────
# Streaming-TTS "chunking": tokens → speakable sentence chunks
#
# The latency win: instead of waiting for the WHOLE answer before synthesizing
# speech, we flush a chunk to the TTS engine the instant a natural pause point
# appears. Piper can start voicing sentence 1 while Ollama is still writing
# sentence 2 — first-audio latency drops from "full reply time" to "first
# sentence time".
# ─────────────────────────────────────────────────────────────────────────────
# Hard sentence ends — always a safe place to flush and speak.
_SENTENCE_BOUNDARIES = frozenset(".!?\n")
# Soft pauses — also flushable, but only once the chunk is long enough so we
# don't hand the synth useless micro-fragments like "Yes," (choppy + wasteful).
_SOFT_BOUNDARIES = frozenset(",;:")
_MIN_SOFT_FLUSH_CHARS = int(os.getenv("NEXUS_TTS_MIN_CHUNK", "24"))


class SentenceChunker:
    """
    Sentence Boundary Detection for streaming TTS.

    Feed it raw LLM tokens as they arrive (`feed`); it accumulates them in a
    temporary buffer and hands back any COMPLETE speakable chunks the moment a
    boundary is hit — a period/!/?/newline, or a comma/semicolon/colon once the
    buffer is long enough. Call `flush()` at end-of-stream to get the trailing
    partial sentence so nothing is left unspoken.

    Stateful + single-threaded: use one chunker per response.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, token: str) -> list[str]:
        """Add one streamed token; return 0+ finished chunks ready for TTS."""
        self._buf += token
        chunks: list[str] = []
        while True:
            idx = self._find_boundary()
            if idx is None:
                break
            chunk = self._buf[: idx + 1].strip()
            self._buf = self._buf[idx + 1:]   # clear the spoken part, keep the rest
            if chunk:
                chunks.append(chunk)
        return chunks

    def flush(self) -> str | None:
        """End of the LLM stream: return whatever text is left, if any."""
        leftover = self._buf.strip()
        self._buf = ""
        return leftover or None

    def _find_boundary(self) -> int | None:
        for i, ch in enumerate(self._buf):
            if ch in _SENTENCE_BOUNDARIES:
                return i
            # Soft pause: only a boundary once the chunk would be >= the minimum
            # length (i is 0-based, so the chunk length is i + 1).
            if ch in _SOFT_BOUNDARIES and (i + 1) >= _MIN_SOFT_FLUSH_CHARS:
                return i
        return None


async def stream_sentences(messages: list, num_predict: int = OLLAMA_NUM_PREDICT_VOICE):
    """
    Sentence-level wrapper around `stream_chat` for the streaming-TTS pipeline.

    Rather than yielding raw tokens, this buffers them with a SentenceChunker and
    yields whole speakable chunks the instant a sentence boundary appears. The
    caller can therefore kick off TTS synthesis for chunk N while Ollama is still
    generating chunk N+1.

    Nothing here blocks token generation: `stream_chat` already pumps Ollama on a
    worker thread and feeds tokens back through an asyncio.Queue, and the
    buffering below is pure in-memory string work between awaits. The TTS engine
    consumes these chunks on its own thread/queue, so flushing a chunk never
    stalls the continued reading of LLM tokens.

    Yields either a chunk `str`, or a `("__error__", msg)` tuple passed straight
    through from `stream_chat` so the caller can surface LLM failures unchanged.
    """
    chunker = SentenceChunker()
    async for token in stream_chat(messages, num_predict=num_predict):
        if isinstance(token, tuple):     # ("__error__", msg) — propagate, then stop
            yield token
            return
        for chunk in chunker.feed(token):
            yield chunk                  # flushed immediately → TTS starts now
    tail = chunker.flush()               # speak any trailing partial sentence
    if tail:
        yield tail


# ═════════════════════════════════════════════════════════════════════════════
# Two-Stage Fast Triage + Streaming generation
#
# The latency problem: every turn paid for RAG (Ollama embed → Chroma query →
# Postgres/vault assembly) BEFORE the first answer token, even for "hi" or
# "what is an API?". That round-trip is dead time the user hears as silence.
#
# The fix is a two-stage pipeline that mimics how a human assistant behaves:
#
#   Stage 1 — TRIAGE (tiny, context-free, ~1 token):
#       Ask the LLM one cheap question: does answering this NEED the user's
#       screen/codebase, or is it general/casual? No RAG, no history, num_predict
#       clamped to a few tokens so it returns almost instantly.
#
#   Stage 2 — BRANCH:
#       • Branch A (CONTEXT): speak a human "filler" line IMMEDIATELY
#         ("Let me check that for you…") so audio starts at once, and
#         CONCURRENTLY assemble the heavy RAG context in a background task. When
#         the deep prompt is ready, stream the grounded answer.
#       • Branch B (CASUAL): skip RAG entirely and stream the answer directly.
#
#   Both branches feed the SAME SentenceChunker, so whichever answer is flowing
#   gets flushed to TTS sentence-by-sentence the instant a boundary appears.
#
# Design note: triage returns a one-word CLASS (CONTEXT/CASUAL) and the SERVER
# owns the exact filler wording. Asking the model to emit a verbatim filler
# phrase for the server to string-match would be fragile (wording drifts) and
# waste tokens — a deterministic class + canned filler is faster and reliable.
# ═════════════════════════════════════════════════════════════════════════════
# Few-shot triage prompt. A 1.5B model is a weak zero-shot classifier, so we (a)
# give it labelled examples and (b) run it at temperature 0 for determinism. The
# deterministic keyword fast-path below catches the obvious cases this size of
# model still gets wrong (e.g. "fix this function" vs "what is an API?").
TRIAGE_SYSTEM_PROMPT = (
    "You are an intent classifier for Nexus, a screen-aware coding assistant. "
    "Classify the user's message as CONTEXT or CASUAL.\n"
    "CONTEXT = answering needs the user's screen, open file, or codebase on user's pc.\n"
    "CASUAL  = answerable from general knowledge, no screen needed.\n\n"
    "Examples:\n"
    "'what file is open?' -> CONTEXT\n"
    "'fix this function' -> CONTEXT\n"
    "'explain this error' -> CONTEXT\n"
    "'why is my code failing?' -> CONTEXT\n"
    "'what am I working on?' -> CONTEXT\n"
    "'hi there' -> CASUAL\n"
    "'thanks!' -> CASUAL\n"
    "'what is an API?' -> CASUAL\n"
    "'who are you?' -> CASUAL\n"
    "'tell me a joke' -> CASUAL\n\n"
    "Reply with EXACTLY ONE WORD — CONTEXT or CASUAL. Nothing else."
)

# Human "filler" spoken instantly on a CONTEXT turn while RAG assembles behind it.
# A small rotating pool so repeated turns don't sound like a robot saying the exact
# same line every time. Set NEXUS_FILLER_PHRASE to force ONE fixed phrase instead.
FILLER_PHRASES = [
    "Sure, let me check your data — give me just a moment.",
    "One moment please, let me take a look at that file for you.",
    "Okay, hold on a second while I check what's on your screen.",
    "Let me look into that for you, just give me a moment.",
    "Alright, checking your workspace now — one second please.",
]
_FILLER_OVERRIDE = os.getenv("NEXUS_FILLER_PHRASE", "").strip()

# Deterministic "Not Visible" reply, sent WITHOUT calling the LLM when a context
# turn has no active file AND no relevant workspace code — a tiny model can't be
# trusted to decide this from prose, so the server decides it in code. The two
# markers below are emitted by retrieve_user_context for the visible-file and
# RAG-hit cases respectively; their absence means "we genuinely have no data".
NOT_VISIBLE_REPLY = "Please open that file on your screen so I can see its data."
_ACTIVE_FILE_MARKER = "Its exact contents are:"
_RAG_MARKER = "[BACKGROUND REFERENCE"


def _pick_filler() -> str:
    """A natural filler line for a CONTEXT turn. Honours the env override (fixed
    phrase) when set; otherwise rotates the pool so it sounds human, not canned."""
    return _FILLER_OVERRIDE or random.choice(FILLER_PHRASES)
# Hard cap on triage output — we only need one word.
TRIAGE_NUM_PREDICT = int(os.getenv("NEXUS_TRIAGE_NUM_PREDICT", "4"))

# Deterministic fast-path signals (lowercased substring match). These let us skip
# the LLM entirely for high-confidence cases — faster AND more reliable than a
# tiny model. Deixis ("this", "here") + code/work verbs ⇒ they're pointing at the
# screen; greetings + "what is a/an …" general-knowledge openers ⇒ casual.
_CONTEXT_SIGNALS = (
    "this file", "this code", "this function", "this error", "this bug",
    "this line", "open file", "on screen", "on my screen", "my code",
    "current file", "the file", "fix this", "fix the", "refactor", "debug",
    "what's wrong", "whats wrong", "why is my", "what am i working",
    "explain this", "this method", "this class", "the function above",
)
_CASUAL_SIGNALS = (
    "what is a", "what is an", "what are", "who are you", "tell me a joke",
    "how do you", "thank", "hello", "good morning", "good evening",
)
_GREETINGS = {"hi", "hey", "yo", "hiya", "sup", "hello", "thanks", "ok", "okay"}


def _heuristic_intent(q: str) -> bool | None:
    """Cheap deterministic triage. Returns True (CONTEXT) / False (CASUAL), or
    None if it can't decide confidently (→ fall back to the LLM)."""
    low = q.lower().strip()
    if low.strip(" .,!?") in _GREETINGS:
        return False
    if any(sig in low for sig in _CONTEXT_SIGNALS):
        return True
    if any(low.startswith(sig) or sig in low for sig in _CASUAL_SIGNALS):
        return False
    return None


def _triage_llm_sync(question: str) -> str:
    """Blocking single-shot triage call (temperature 0). Returns the raw verdict
    text, or '' on failure. Run via run_in_executor so it never blocks the loop."""
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"num_predict": TRIAGE_NUM_PREDICT, "temperature": 0},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return (data.get("message") or {}).get("content", "") or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("triage LLM call failed: %s", exc)
        return ""


async def triage_intent(question: str) -> bool:
    """
    Stage 1: ultra-fast intent classification. Returns True if the question needs
    screen/codebase context (Branch A), False if it's casual/general (Branch B).

    Two tiers: a deterministic keyword fast-path (zero latency, handles the
    obvious cases), then a temperature-0 few-shot LLM call for the ambiguous
    middle. Fails SAFE: an empty/garbled LLM verdict defaults to True (load
    context) — wrongly running RAG on a casual question just costs a little time,
    but skipping context on a code question gives a blind, wrong answer.
    """
    fast = _heuristic_intent(question)
    if fast is not None:
        log.info("🧭 triage(fast)=%s for %r", "CONTEXT" if fast else "CASUAL", question[:60])
        return fast

    loop = asyncio.get_running_loop()
    verdict = (await loop.run_in_executor(None, _triage_llm_sync, question)).strip().upper()
    log.info("🧭 triage(llm)=%r for %r", verdict, question[:60])
    # Only skip context when the model clearly says CASUAL; default to context.
    return "CASUAL" not in verdict


def _chunk_static(text: str) -> list:
    """Split a fixed string (e.g. the filler line) into speakable TTS chunks
    using the SAME boundary logic as live generation, so it sounds consistent."""
    chunker = SentenceChunker()
    chunks = chunker.feed(text)
    tail = chunker.flush()
    if tail:
        chunks.append(tail)
    return chunks


async def generate_reply(
    username: str,
    question: str,
    history: list,
    *,
    active: dict | None = None,
    voice: bool = False,
):
    """
    The full two-stage turn as a single async generator. The WebSocket dispatcher
    (server.py) consumes the typed events and routes them to the client:

        ("filler",   str)  — a canned human filler chunk; speak it NOW (Branch A)
        ("token",    str)  — one raw answer token, for live on-screen text
        ("speak",    str)  — a completed sentence chunk, for the TTS subsystem
        ("answer",   str)  — emitted once at the end: the full answer text
        ("error",    str)  — LLM/pipeline failure

    Nothing here blocks the event loop: stream_chat pumps Ollama on a worker
    thread, RAG assembly runs as a concurrent asyncio.Task, and the chunking is
    pure string work between awaits.
    """
    num_predict = OLLAMA_NUM_PREDICT_VOICE if voice else OLLAMA_NUM_PREDICT_TEXT

    # ── Stage 1: triage (no context load) ────────────────────────────────────
    needs_context = await triage_intent(question)

    # ── Stage 2: branch ──────────────────────────────────────────────────────
    if needs_context:
        # BRANCH A — fire RAG assembly in the BACKGROUND immediately, then speak
        # the filler so the user hears something while Postgres/Chroma work runs.
        rag_task = asyncio.create_task(
            retrieve_user_context(username, question, active=active)
        )
        # Flush the filler to TTS at once (sentence-chunked for consistency).
        for chunk in _chunk_static(_pick_filler()):
            yield ("filler", chunk)
        # Now await the heavy context that's been assembling concurrently.
        try:
            user_content = await rag_task
        except Exception as exc:  # noqa: BLE001 — RAG must never kill the turn
            log.warning("RAG assembly failed, falling back to bare question: %s", exc)
            user_content = question

        # The "Not Visible" fallback — decided in CODE, not by the model. If a
        # context turn produced neither a visible active file nor any relevant
        # workspace code, we have nothing real to ground an answer in, so we ask
        # the user to open the file instead of letting the LLM hallucinate one.
        if _ACTIVE_FILE_MARKER not in user_content and _RAG_MARKER not in user_content:
            log.info("🚫 no visible file/context — asking user to open the file.")
            for chunk in _chunk_static(NOT_VISIBLE_REPLY):
                yield ("speak", chunk)
            yield ("token", NOT_VISIBLE_REPLY)
            yield ("answer", NOT_VISIBLE_REPLY)
            return
    else:
        # BRANCH B — casual/general: skip RAG entirely, answer straight away.
        user_content = question

    messages = build_messages(list(history), user_content, voice=voice)

    # ── Stage 3: stream the answer, chunked to sentences for TTS ──────────────
    chunker = SentenceChunker()
    parts: list[str] = []
    async for token in stream_chat(messages, num_predict=num_predict):
        if isinstance(token, tuple):              # ("__error__", msg)
            yield ("error", token[1])
            return
        parts.append(token)
        yield ("token", token)                    # live text for the UI
        for chunk in chunker.feed(token):
            yield ("speak", chunk)                # flushed sentence → TTS now
    tail = chunker.flush()
    if tail:
        yield ("speak", tail)
    yield ("answer", "".join(parts))


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
