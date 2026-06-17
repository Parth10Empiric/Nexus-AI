#!/usr/bin/env python3
"""
run_nexus.py — single entry point that connects the entire Nexus AI backend.

Starts BOTH halves of the system in one process so the desktop app has
everything it needs:

  * The Tracker daemon (background thread): window tracking + watchdog file
    reading + live vector indexing on save (the "eyes" + memory).
  * The Session Orchestrator (main asyncio loop): wake word, STT, hybrid
    retrieval, Ollama, TTS, and the WebSocket bridge the React/Tauri dashboard
    connects to for the live agent toggle and state (the "brain" + "voice").

Both share the ONE NexusMemoryManager singleton, so what the tracker indexes is
instantly what the orchestrator retrieves.

It ALSO boots the client-server "Brain" (server.py / FastAPI WebSocket gateway)
in the same event loop, so the streaming-TTS + RAG pipeline that remote React/
Tauri clients use is exercised by the one command you already test with. Disable
it with NEXUS_RUN_BRAIN_SERVER=0; change its port with NEXUS_BRAIN_PORT.

Run the backend:        python run_nexus.py
Run the desktop app:    cd frontend && npm run tauri:dev   (connects via WebSocket)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from tracker.session_orchestrator import SessionOrchestrator
from tracker.tracker import Tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus.run")

# Whether to also serve the client-server Brain (server.py) from this process.
RUN_BRAIN_SERVER = os.getenv("NEXUS_RUN_BRAIN_SERVER", "1") not in ("0", "false", "no")
BRAIN_HOST = os.getenv("NEXUS_BRAIN_HOST", "0.0.0.0")
BRAIN_PORT = int(os.getenv("NEXUS_BRAIN_PORT", "8000"))


async def _serve_brain() -> None:
    """Run server.py's FastAPI app inside THIS asyncio loop (no reload, so it
    coexists with the orchestrator). Cancellation triggers a graceful shutdown."""
    import uvicorn

    config = uvicorn.Config("server:app", host=BRAIN_HOST, port=BRAIN_PORT, log_level="info")
    server = uvicorn.Server(config)
    log.info("🧠 Brain server starting on ws://%s:%d/ws", BRAIN_HOST, BRAIN_PORT)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True   # let uvicorn unwind its own tasks cleanly
        raise

async def _main() -> int:
    # 1. Tracker daemon on a background (daemon) thread — eyes + memory.
    tracker = Tracker()
    t = threading.Thread(target=tracker.run, name="nexus-tracker", daemon=True)
    t.start()
    log.info("🟢 Tracker daemon started (window tracking + watchdog + auto-index).")

    # 2. Brain server (FastAPI WebSocket gateway) as a concurrent loop task.
    brain_task: asyncio.Task | None = None
    if RUN_BRAIN_SERVER:
        brain_task = asyncio.create_task(_serve_brain(), name="nexus-brain")

    # 3. Session orchestrator in the main asyncio loop — brain + voice + UI bridge.
    orch = SessionOrchestrator()
    try:
        await orch.run()           # serves ws://127.0.0.1:8765 for the dashboard
    finally:
        tracker._running = False   # signal the tracker thread to stop
        t.join(timeout=3)
        if brain_task is not None:
            brain_task.cancel()
            try:
                await brain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutting down
                pass
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Nexus AI shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
