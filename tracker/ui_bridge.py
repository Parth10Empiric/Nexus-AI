"""
ui_bridge.py — Phase 4.5 state synchronization between Python and the React UI.

A tiny local WebSocket server. The React "Voice Agent Mode" toggle connects to
it, sends commands ({"cmd": "activate"} / {"cmd": "deactivate"}), and receives
real-time state events ({"type": "state", "state": "listening"|"thinking"|
"speaking"|"standby"|"off"}) so the dashboard's indicator always mirrors what
Nexus Ten is doing.

We use a local WebSocket (not Tauri IPC) because the Python orchestrator is a
separate process from the Tauri/React app — a socket is the clean cross-process
channel both sides speak.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional, Set

import websockets

from . import config

log = logging.getLogger("nexus.tracker.ui")

CommandHandler = Callable[[dict], Awaitable[None]]


class UIBridge:
    def __init__(
        self,
        on_command: CommandHandler,
        host: str = config.UI_WS_HOST,
        port: int = config.UI_WS_PORT,
    ) -> None:
        self._on_command = on_command
        self._host = host
        self._port = port
        self._clients: Set = set()
        self._server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_state = "off"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await websockets.serve(self._handler, self._host, self._port)
        log.info("UI bridge listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- per-connection handler --------------------------------------------
    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        log.info("UI client connected (%d total).", len(self._clients))
        try:
            # Greet the new client with the current state so it syncs instantly.
            await ws.send(json.dumps({"type": "state", "state": self._last_state}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._on_command(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            log.info("UI client disconnected (%d left).", len(self._clients))

    # -- broadcasting -------------------------------------------------------
    async def emit(self, payload: dict) -> None:
        """Broadcast any JSON payload to every connected UI client."""
        if not self._clients:
            return
        data = json.dumps(payload)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def emit_state(self, state: str, detail: str = "") -> None:
        """Broadcast a state change to every connected UI client."""
        self._last_state = state
        await self.emit({"type": "state", "state": state, "detail": detail})

    def emit_threadsafe(self, payload: dict) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.emit(payload), self._loop)

    def emit_state_threadsafe(self, state: str, detail: str = "") -> None:
        """Schedule an emit from a non-asyncio thread (e.g. the audio callback)."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self.emit_state(state, detail), self._loop
            )
