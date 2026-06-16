import { useCallback, useEffect, useState } from "react";

/**
 * useOsContext — Phase 6.1 "Client Eyes" bridge.
 *
 * Listens to the unified `nexus://os-context` event emitted by the native Rust
 * core (src-tauri/src/main.rs) and exposes the latest snapshot to React. This
 * is what replaces the old Python tracker → it works fully offline inside the
 * distributed .deb, with no backend service running.
 *
 * The event fires on TWO triggers, both delivering the SAME payload shape:
 *   1. Every 1000ms   — the background active-window poller ticks.
 *   2. On every save  — once `startFileWatcher(path)` has been called.
 *
 * Payload (contract — mirrors `OsContextPayload` in Rust):
 *   {
 *     timestamp:            "2026-06-16T10:22:31.482+00:00",
 *     active_window_title:  "main.rs — Nexus AI — VS Code",
 *     active_app_name:      "code",
 *     last_saved_file_name: "main.rs" | null,
 *     file_content:         "use std::..." | null
 *   }
 *
 * Returns:
 *   context        -> the latest payload object | null (until first tick)
 *   startFileWatcher(path) -> Promise — begins watching a workspace recursively
 *   isTauri        -> false in a plain `vite dev` browser (no native core)
 */

function isTauriEnv() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function useOsContext() {
  const [context, setContext] = useState(null);

  // Subscribe to the global Rust event bus for the lifetime of the component.
  useEffect(() => {
    if (!isTauriEnv()) return; // browser dev mode: nothing to listen to

    let unlisten = null;
    let cancelled = false;

    import("@tauri-apps/api/event").then(({ listen }) => {
      if (cancelled) return;
      listen("nexus://os-context", (event) => {
        // VERIFY: prove the native payload is arriving exactly as specified.
        console.log("[nexus://os-context]", event.payload);
        setContext(event.payload);
      }).then((fn) => {
        if (cancelled) fn(); // component unmounted before listener resolved
        else unlisten = fn;
      });
    });

    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, []);

  /**
   * Ask the Rust core to begin recursively watching `workspacePath` (may start
   * with `~`). Resolves on success; rejects with the Rust error string if the
   * path does not exist.
   */
  const startFileWatcher = useCallback(async (workspacePath) => {
    if (!isTauriEnv()) return; // no-op in the browser
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke("start_file_watcher", { workspacePath });
  }, []);

  return { context, startFileWatcher, isTauri: isTauriEnv() };
}
