#!/usr/bin/env python3
"""
server.py — Phase 7.1: Nexus AI central "Brain" server (WebSocket gateway).

The first slice of the Client-Server SaaS migration. This FastAPI app accepts
WebSocket connections from remote React/Tauri clients (tunnelled in via ngrok),
authenticates each one with an "Invite Key" handshake, and tracks every live
client in an in-memory registry keyed by username.

What this file is responsible for (Phase 7.1 scope):
  * The auth handshake  — first frame MUST be {"type": "auth", "invite_key": ...}
  * The ConnectionManager — one source of truth for who is online + isolated
    per-user message routing.
  * The /ws endpoint    — handshake -> register -> listen loop -> clean teardown.

NOT in scope yet (later phases): the actual LLM/voice/memory pipeline, and the
Postgres-backed user store (Phase 7.3 replaces VALID_USERS).

Run it:   python server.py        (serves ws://0.0.0.0:8000/ws)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Dict, TypedDict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

# Phase 7.2 storage layer + Phase 7.3 brain. Imported lazily-safe: these modules
# build engines/clients at import but open no network connections until used.
import brain_service
from data_service import process_incoming_file_sync

# Rolling conversation history kept per connection (mirrors the local app's 10).
HISTORY_TURNS = 10

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus.server")

# ─────────────────────────────────────────────────────────────────────────────
# In-memory auth registry (Phase 7.1 baseline — replaced by Postgres in 7.3)
#
# Maps an opaque Invite Key -> the username it belongs to. Anyone holding a key
# can connect AS that user. Keep these secret; rotate by editing this dict.
# ─────────────────────────────────────────────────────────────────────────────
VALID_USERS: Dict[str, str] = {
    "nexus_key_44bB": "friend_a",
    "nexus_key_99xA": "friend_b",
}

# How long (seconds) we wait for the client's first (auth) frame before giving
# up and closing the socket. Stops idle/half-open sockets from lingering.
AUTH_TIMEOUT_SECONDS = 10.0

# WebSocket close codes (RFC 6455).
WS_POLICY_VIOLATION = 1008  # auth failed / timed out / malformed handshake


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models — validate the handshake payload strictly.
# ─────────────────────────────────────────────────────────────────────────────
class AuthMessage(BaseModel):
    """The mandatory first frame: {"type": "auth", "invite_key": "nexus_key_..."}."""

    type: str
    invite_key: str


# Shape of one entry in the live registry. Documents the dict for readers/tools.
class Connection(TypedDict):
    websocket: WebSocket
    status: str


# ─────────────────────────────────────────────────────────────────────────────
# ConnectionManager — the single source of truth for "who is online".
# ─────────────────────────────────────────────────────────────────────────────
class ConnectionManager:
    """
    Owns every live WebSocket, grouped by username.

    The registry maps each user to the SET of their currently-open sockets:
        ACTIVE_CONNECTIONS = { "friend_a": {<ws1>, <ws2>, ...} }

    Why a set (not one socket): a single client app legitimately opens several
    sockets — the dashboard, the chat panel, and the floating orb each connect.
    They are all the SAME tenant and must stay in lock-step, so we keep them all
    and BROADCAST every personal message to all of that user's sockets (mirrors
    the old UI bridge). Evicting "duplicates" would make the windows fight each
    other in an endless reconnect loop.

    Isolation is preserved at the username level: a message for `friend_a` only
    ever reaches `friend_a`'s sockets, never another tenant's.
    """

    def __init__(self) -> None:
        self.active_connections: Dict[str, set] = {}

    async def connect(self, websocket: WebSocket, username: str) -> None:
        """Register an ALREADY-AUTHENTICATED socket under `username`."""
        self.active_connections.setdefault(username, set()).add(websocket)
        log.info(
            "✅ %r connected (%d socket(s)). Users online: %d",
            username,
            len(self.active_connections[username]),
            len(self.active_connections),
        )

    def disconnect(self, websocket: WebSocket, username: str) -> None:
        """Remove ONE socket. Drops the user entry when their last socket goes."""
        conns = self.active_connections.get(username)
        if not conns:
            return
        conns.discard(websocket)
        if not conns:
            self.active_connections.pop(username, None)
        log.info("👋 %r socket closed. Users online: %d", username, len(self.active_connections))

    async def send_personal_message(self, message: dict, username: str) -> bool:
        """
        Broadcast `message` (JSON) to ALL of this user's sockets. Returns True if
        it reached at least one. Dead sockets are pruned. Safe for offline users.
        """
        conns = list(self.active_connections.get(username, ()))
        if not conns:
            return False
        delivered = 0
        for ws in conns:
            try:
                await ws.send_json(message)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — socket died mid-send
                log.warning("Send to %r failed (%s); pruning.", username, exc)
                self.disconnect(ws, username)
        return delivered > 0


# Single app-wide manager instance.
manager = ConnectionManager()

app = FastAPI(title="Nexus AI Brain", version="7.1.0")


@app.get("/")
async def health() -> dict:
    """Lightweight health/liveness probe (handy through the ngrok tunnel)."""
    return {"service": "nexus-brain", "phase": "7.1", "online_users": len(manager.active_connections)}


# ─────────────────────────────────────────────────────────────────────────────
# Authentication handshake
# ─────────────────────────────────────────────────────────────────────────────
async def _authenticate(websocket: WebSocket) -> str | None:
    """
    Perform the mandatory auth handshake on a freshly-accepted socket.

    Protocol: the client's VERY FIRST frame must be
        {"type": "auth", "invite_key": "nexus_key_..."}

    Returns the resolved `username` on success, or `None` if the socket should
    be rejected. On any failure path the socket is closed here with 1008 so the
    endpoint can simply `return`.
    """
    try:
        # Wait for the first frame, but never block forever on a silent client.
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=AUTH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("Auth timed out after %.0fs — closing.", AUTH_TIMEOUT_SECONDS)
        await websocket.close(code=WS_POLICY_VIOLATION, reason="auth timeout")
        return None
    except (WebSocketDisconnect, ValueError):
        # Client vanished, or sent non-JSON garbage as its first frame.
        log.warning("Malformed/early-closed handshake — closing.")
        await _safe_close(websocket, reason="malformed handshake")
        return None

    # Validate shape: must be a well-formed auth message.
    try:
        auth = AuthMessage(**raw)
    except (ValidationError, TypeError):
        log.warning("First frame was not a valid auth payload — closing.")
        await websocket.close(code=WS_POLICY_VIOLATION, reason="expected auth frame")
        return None

    if auth.type != "auth":
        log.warning("First frame type was %r, expected 'auth' — closing.", auth.type)
        await websocket.close(code=WS_POLICY_VIOLATION, reason="expected auth frame")
        return None

    # The actual key check against our registry.
    username = VALID_USERS.get(auth.invite_key)
    if username is None:
        log.warning("Rejected invalid invite key.")
        await websocket.close(code=WS_POLICY_VIOLATION, reason="invalid invite key")
        return None

    return username


async def _safe_close(websocket: WebSocket, reason: str = "") -> None:
    """Best-effort close that never raises even if the socket is already gone."""
    try:
        await websocket.close(code=WS_POLICY_VIOLATION, reason=reason)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7.3 — Brain handlers (preserve the existing frontend message protocol)
#
# Outgoing to client:  {"type":"user","text"} {"type":"token","token"}
#                      {"type":"answer","text"} {"type":"state","state",...}
# Incoming from client: {"cmd":"ask","text"} {"cmd":"activate"|...}
#                       {"type":"file_sync",...} {"type":"audio_end"} + binary PCM
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_ask(
    username: str, text: str, history: deque, *, voice: bool = False, active: dict | None = None
) -> None:
    """
    Run one turn: retrieve per-tenant context, stream the LLM answer.

    `voice=True` (spoken input) drives the voice-agent state machine
    (thinking → back to listening so the conversation loops). Text chat does NOT
    emit voice states — it only streams user/token/answer — so typing in the
    chat panel never flips the Voice Agent toggle.
    """
    text = (text or "").strip()
    if not text:
        return

    # Print the user's question to the server terminal, tagged with the user.
    log.info("🗣️  [%s] ASK: %s", username, text)

    await manager.send_personal_message({"type": "user", "text": text}, username)
    # Note: the voice path already emitted "thinking" before STT, so we don't
    # re-emit it here — keeps the state sequence exact (no double "thinking").

    # Per-tenant retrieval → prompt grounded in the user's OPEN file + vault.
    user_content = await brain_service.retrieve_user_context(username, text, active=active)
    messages = brain_service.build_messages(list(history), user_content, voice=voice)

    # Voice answers are short (fast + small TTS); typed answers can be longer.
    num_predict = (
        brain_service.OLLAMA_NUM_PREDICT_VOICE if voice else brain_service.OLLAMA_NUM_PREDICT_TEXT
    )

    parts: list[str] = []
    async for token in brain_service.stream_chat(messages, num_predict=num_predict):
        if isinstance(token, tuple):  # ("__error__", msg)
            log.warning("⚠️  [%s] LLM error: %s", username, token[1])
            await manager.send_personal_message(
                {"type": "answer", "text": f"⚠️ LLM error: {token[1]}"}, username
            )
            return
        parts.append(token)
        await manager.send_personal_message({"type": "token", "token": token}, username)

    answer = "".join(parts)
    log.info("🤖 [%s] REPLY: %s", username, answer)
    await manager.send_personal_message({"type": "answer", "text": answer}, username)

    if voice:
        # Speak the answer: synthesize on the server, stream PCM to the client.
        await manager.send_personal_message({"type": "state", "state": "speaking"}, username)
        tts = await brain_service.synthesize_tts(answer)
        if tts:
            pcm_b64, sample_rate = tts
            log.info("🔊 [%s] speaking %d chars of audio", username, len(pcm_b64))
            await manager.send_personal_message(
                {"type": "tts_audio", "sample_rate": sample_rate, "pcm_b64": pcm_b64},
                username,
            )

    # Remember the turn (bounded rolling window).
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})


async def _handle_file_sync(username: str, msg: dict) -> None:
    """Persist a saved file to BOTH databases for this tenant (Phase 7.2)."""
    file_path = msg.get("file_path") or ""
    file_content = msg.get("file_content") or ""
    if not file_path:
        return
    loop = asyncio.get_running_loop()
    try:
        # process_incoming_file_sync is blocking (DB + embedding) → off-thread.
        result = await loop.run_in_executor(
            None, process_incoming_file_sync, username, file_path, file_content
        )
        await manager.send_personal_message(
            {"type": "file_synced", "file_path": file_path, **result}, username
        )
    except Exception as exc:  # noqa: BLE001 — storage down shouldn't kill the socket
        log.warning("file_sync failed for %r: %s", username, exc)
        await manager.send_personal_message(
            {"type": "file_synced", "file_path": file_path, "saved": False, "error": str(exc)},
            username,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    Lifecycle: accept → authenticate → register → listen loop → clean teardown.

    Steps:
      1. accept() the TCP/WebSocket upgrade.
      2. _authenticate() consumes the first frame and resolves a username (or
         closes the socket and returns None).
      3. manager.connect() registers the live socket.
      4. The `while True` loop receives client frames and ACKs them. The real
         LLM/voice routing lands here in a later phase.
      5. WebSocketDisconnect (or any error) always runs disconnect() in finally.
    """
    await websocket.accept()

    username = await _authenticate(websocket)
    if username is None:
        return  # _authenticate already closed the socket with 1008.

    await manager.connect(websocket, username)

    # Confirm the handshake to THIS socket only (broadcasting would reset the
    # state of the user's other windows every time a new one connects). Default
    # the agent to OFF so the Voice Agent toggle starts clean and clickable.
    await websocket.send_json(
        {"type": "auth_ok", "username": username, "message": f"Welcome, {username}."}
    )
    await websocket.send_json({"type": "state", "state": "off"})

    # Per-connection state: rolling chat history, accumulating mic PCM buffer,
    # and whether the user has the voice agent ARMED. `armed` is the source of
    # truth for re-arming: after a spoken reply we only return to "listening" if
    # the user hasn't turned the agent off in the meantime — that's what stops
    # the agent auto-restarting after you stop it.
    history: deque = deque(maxlen=HISTORY_TURNS * 2)  # user+assistant per turn
    audio_buffer = bytearray()
    armed = False
    # Latest screen/file context the client's native "eyes" reported. Injected
    # into every prompt so the agent can answer about the currently open file.
    active_ctx: dict = {}

    # Ignore utterances shorter than ~0.4s (12800 bytes @16kHz/16-bit). This
    # kills whisper "hallucinations" on a stray click or breath of silence.
    MIN_PCM_BYTES = 12800

    async def _rearm_or_idle() -> None:
        """After a voice turn, go back to listening ONLY if still armed."""
        audio_buffer.clear()
        await manager.send_personal_message(
            {"type": "state", "state": "listening" if armed else "off"}, username
        )

    try:
        while True:
            # Unified receive: text frames are JSON control; binary frames are
            # raw Int16 PCM mic chunks (Phase 6.2 voice-in stream).
            frame = await websocket.receive()

            if frame.get("type") == "websocket.disconnect":
                break

            # ── Binary frame → accumulate audio (only while armed) ───────────
            if frame.get("bytes") is not None:
                if armed:
                    audio_buffer.extend(frame["bytes"])
                continue

            # ── Text frame → parse JSON and dispatch ─────────────────────────
            text = frame.get("text")
            if not text:
                continue
            try:
                msg = json.loads(text)
            except (ValueError, TypeError):
                log.warning("Non-JSON text frame from %r — ignoring.", username)
                continue

            cmd = msg.get("cmd")
            mtype = msg.get("type")
            log.info("📩 %r -> cmd=%s type=%s armed=%s", username, cmd, mtype, armed)

            if mtype == "os_context":
                # Live screen context from the client's native eyes. Two kinds:
                #   • focus/save events carry a window_title → update "open file"
                #   • initial-scan events have NO window → embed only (don't let a
                #     scanned file masquerade as the user's open file)
                wt = msg.get("window_title") or ""
                fc = msg.get("file_content")
                fp = msg.get("file_path") or msg.get("file_name")
                if wt:
                    active_ctx = {
                        "window_title": wt,
                        "app_name": msg.get("app_name") or "",
                        "file_name": msg.get("file_name") or active_ctx.get("file_name", ""),
                        "file_content": fc if fc else active_ctx.get("file_content", ""),
                    }
                if fc and fp:
                    await _handle_file_sync(username, {"file_path": fp, "file_content": fc})
                continue

            if cmd == "ask":
                # Text chat — answers about the open file too (active_ctx).
                await _handle_ask(username, msg.get("text", ""), history, active=active_ctx)

            elif cmd == "activate":
                # Arm the voice agent → client starts streaming the mic.
                armed = True
                audio_buffer.clear()
                await manager.send_personal_message({"type": "state", "state": "listening"}, username)
            elif cmd == "deactivate":
                # Disarm → OFF. This must STICK even if a turn is mid-flight.
                armed = False
                audio_buffer.clear()
                await manager.send_personal_message({"type": "state", "state": "off"}, username)
            elif cmd == "sleep":
                armed = False
                audio_buffer.clear()
                await manager.send_personal_message({"type": "state", "state": "sleeping"}, username)
            elif cmd == "interrupt":
                audio_buffer.clear()
                await manager.send_personal_message(
                    {"type": "state", "state": "listening" if armed else "off"}, username
                )

            elif mtype == "file_sync":
                await _handle_file_sync(username, msg)

            elif mtype == "audio_end":
                # End-of-utterance. Ignore entirely if the user disarmed.
                pcm = bytes(audio_buffer)
                audio_buffer.clear()
                if not armed:
                    continue
                if len(pcm) < MIN_PCM_BYTES:
                    # Too short to be real speech — stay listening, don't bother STT.
                    log.info("… [%s] utterance too short (%d B) — ignored", username, len(pcm))
                    await manager.send_personal_message({"type": "state", "state": "listening"}, username)
                    continue

                await manager.send_personal_message({"type": "state", "state": "thinking"}, username)
                transcript = (await brain_service.transcribe_pcm16(pcm)).strip()
                if transcript and armed:
                    await _handle_ask(username, transcript, history, voice=True, active=active_ctx)
                    await _rearm_or_idle()
                else:
                    if not transcript:
                        log.info("… [%s] empty transcript — ignored", username)
                    await _rearm_or_idle()

    except WebSocketDisconnect:
        log.info("Socket closed by %r.", username)
    except Exception as exc:  # noqa: BLE001 — defensive: never leak a live entry
        log.exception("Unexpected error in session for %r: %s", username, exc)
    finally:
        # CRITICAL: always remove THIS socket so the registry never holds a ghost.
        manager.disconnect(websocket, username)


if __name__ == "__main__":
    import uvicorn

    # host=0.0.0.0 so ngrok (or any tunnel) can reach it from outside the host.
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
