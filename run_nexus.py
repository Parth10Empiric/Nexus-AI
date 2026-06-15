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

Run the backend:        python run_nexus.py
Run the desktop app:    cd frontend && npm run tauri:dev   (connects via WebSocket)
"""

from __future__ import annotations

import asyncio
import logging
import threading

from tracker.session_orchestrator import SessionOrchestrator
from tracker.tracker import Tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus.run")


async def _main() -> int:
    # 1. Tracker daemon on a background (daemon) thread — eyes + memory.
    tracker = Tracker()
    t = threading.Thread(target=tracker.run, name="nexus-tracker", daemon=True)
    t.start()
    log.info("🟢 Tracker daemon started (window tracking + watchdog + auto-index).")

    # 2. Session orchestrator in the main asyncio loop — brain + voice + UI bridge.
    orch = SessionOrchestrator()
    try:
        await orch.run()           # serves ws://127.0.0.1:8765 for the dashboard
    finally:
        tracker._running = False   # signal the tracker thread to stop
        t.join(timeout=3)
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        log.info("Nexus AI shutting down.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
