import { useEffect, useRef, useState, useCallback } from "react";

/**
 * useActiveWindow — the single source of truth for "what is the developer
 * looking at right now?".
 *
 * It works in two modes, automatically:
 *
 *   1. NATIVE (inside Tauri): it invokes the Rust command `get_active_window`,
 *      which reads the real `data/local_logs.db` written by the Phase 1.1
 *      tracker daemon.
 *
 *   2. BROWSER (plain `vite dev`, no Rust): it runs a lightweight SIMULATION
 *      loop that mocks window switches every few seconds. This lets us prove
 *      the UI is responsive and layout-stable without the native shell — and
 *      satisfies the "Simulation Loop" requirement.
 *
 * Crucially, every state update is NON-BLOCKING: we poll on a timer and call
 * setState with already-resolved data. We never do synchronous heavy work on
 * the render thread, so the UI never stutters.
 *
 * Returns: { entry, status, lastUpdate }
 *   entry      -> { appName, title, timestamp, event } | null
 *   status     -> "online" | "offline"
 *   lastUpdate -> epoch ms of the last successful read (for the "live" feel)
 */

const POLL_MS = 2000;

// Mock timeline used when no native backend is present.
const MOCK_SEQUENCE = [
  {
    appName: "code",
    title: "useActiveWindow.js — Nexus AI — Visual Studio Code",
    event: "switch",
  },
  {
    appName: "chrome",
    title:
      "Tauri v2 Docs — Inter-Process Communication (invoke) — Google Chrome",
    event: "switch",
  },
  {
    appName: "gnome-terminal",
    title: "empiric@elitedesk: ~/Projects/Nexus AI/frontend",
    event: "switch",
  },
  {
    appName: "code",
    title:
      "/home/empiric/Projects/Nexus AI/frontend/src/components/ActiveWindowCard.jsx — Visual Studio Code",
    event: "switch",
  },
  {
    appName: "chrome",
    title:
      "How did I fix that JWT refresh-token race condition? — Stack Overflow — Google Chrome",
    event: "switch",
  },
  {
    appName: "code",
    title: "tracker.py — Nexus AI — Visual Studio Code",
    event: "heartbeat",
  },
];

function isTauri() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function useActiveWindow() {
  const [entry, setEntry] = useState(null);
  const [status, setStatus] = useState("offline");
  const [lastUpdate, setLastUpdate] = useState(0);

  // Refs survive re-renders without retriggering effects.
  const mockIndex = useRef(0);
  const invokeRef = useRef(null);

  // Lazily load the Tauri invoke fn only when running natively.
  useEffect(() => {
    let cancelled = false;
    if (isTauri()) {
      import("@tauri-apps/api/core").then((mod) => {
        if (!cancelled) invokeRef.current = mod.invoke;
      });
    }
    return () => {
      cancelled = true;
    };
  }, []);

  const readNative = useCallback(async () => {
    try {
      const invoke = invokeRef.current;
      if (!invoke) return; // module still loading
      const row = await invoke("get_active_window");
      if (row) {
        setEntry({
          appName: row.app_name,
          title: row.title,
          timestamp: row.ts_utc,
          event: row.event,
        });
      }
      setStatus("online");
      setLastUpdate(Date.now());
    } catch {
      // DB missing or tracker not running yet -> show OFFLINE, don't crash.
      setStatus("offline");
    }
  }, []);

  const readMock = useCallback(() => {
    const next = MOCK_SEQUENCE[mockIndex.current % MOCK_SEQUENCE.length];
    mockIndex.current += 1;
    setEntry({
      appName: next.appName,
      title: next.title,
      timestamp: new Date().toISOString(),
      event: next.event,
    });
    setStatus("online");
    setLastUpdate(Date.now());
  }, []);

  useEffect(() => {
    const tick = isTauri() ? readNative : readMock;
    tick(); // fire immediately so the UI isn't empty on first paint
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [readNative, readMock]);

  return { entry, status, lastUpdate };
}
