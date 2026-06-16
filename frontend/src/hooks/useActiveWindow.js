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
    // BROWSER: no native shell → run the mock timeline so the UI isn't empty.
    if (!isTauri()) {
      readMock();
      const id = setInterval(readMock, POLL_MS);
      return () => clearInterval(id);
    }

    // NATIVE: subscribe to the Phase 6.1 Rust "eyes" event (nexus://os-context).
    // This is live, needs NO Python tracker, and replaces the old SQLite poll
    // (get_active_window) that showed stale/empty data — the source of the
    // "random names" bug. Each 1000ms tick carries the real focused window.
    let unlisten = null;
    let cancelled = false;
    import("@tauri-apps/api/event").then(({ listen }) => {
      if (cancelled) return;
      listen("nexus://os-context", (event) => {
        const p = event.payload || {};
        // Empty strings happen on unsupported sessions (e.g. some Wayland) —
        // keep the previous entry rather than flashing blank, but stay online.
        if (!p.active_window_title && !p.active_app_name) {
          setStatus("online");
          setLastUpdate(Date.now());
          return;
        }
        setEntry({
          appName: p.active_app_name || "",
          title: p.active_window_title || "",
          timestamp: p.timestamp || new Date().toISOString(),
          event: "live",
        });
        setStatus("online");
        setLastUpdate(Date.now());
      }).then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      });
    });

    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, [readMock]);

  return { entry, status, lastUpdate };
}
