import { useEffect, useRef } from "react";

/**
 * useOsContextSync — Phase 7.3: forward the native "eyes" to the server.
 *
 * The Phase 6.1 Rust core emits `nexus://os-context` (active window + saved-file
 * content) but nothing sent it to the brain server, so the agent couldn't see
 * your screen/files. This hook:
 *   1. starts the recursive file watcher on your workspace (saves stream in,
 *      and the initial scan emits every code file for indexing), and
 *   2. forwards each os-context event to the server as `{type:"os_context",…}`.
 *
 * The server keeps the latest "currently open file" (so it can answer about it)
 * and embeds any file content into your per-tenant ChromaDB vault.
 *
 * Window-only updates are throttled; file-bearing events always go through.
 * Runs in the main window only. Pass the shared socket's `send` + `connected`.
 */
const WORKSPACE = import.meta.env.VITE_NEXUS_WORKSPACE || "";

function isTauri() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function useOsContextSync({ send, connected }) {
  const sendRef = useRef(send);
  const connectedRef = useRef(connected);
  sendRef.current = send;
  connectedRef.current = connected;
  // Signature of the last os_context we sent, so we only send on actual change
  // (the native eyes now emit every second; without this we'd spam the server
  // with identical context and re-embed the same file constantly).
  const lastSig = useRef("");

  useEffect(() => {
    if (!isTauri()) return undefined;
    let unlisten = null;
    let cancelled = false;

    // Start watching the workspace (saves + one-time scan of existing files).
    if (WORKSPACE) {
      import("@tauri-apps/api/core").then(({ invoke }) => {
        invoke("start_file_watcher", { workspacePath: WORKSPACE }).catch((e) =>
          console.warn("[os-context] start_file_watcher failed:", e)
        );
      });
    } else {
      console.warn("[os-context] VITE_NEXUS_WORKSPACE not set — no files will be indexed.");
    }

    import("@tauri-apps/api/event").then(({ listen }) => {
      if (cancelled) return;
      listen("nexus://os-context", (event) => {
        if (!connectedRef.current) return;
        const p = event.payload || {};
        const content = p.file_content || "";

        // Only send when the focus/file actually changed (cheap signature on
        // window + file name + content length). Sitting on an unchanged file
        // sends nothing; switching files or editing+saving sends once.
        const sig = `${p.active_window_title || ""}|${p.last_saved_file_name || ""}|${content.length}`;
        if (sig === lastSig.current) return;
        lastSig.current = sig;

        sendRef.current({
          type: "os_context",
          window_title: p.active_window_title || "",
          app_name: p.active_app_name || "",
          file_name: p.last_saved_file_name || "",
          file_path: p.last_saved_file_name || "",
          file_content: content,
        });
      }).then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      });
    });

    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, []);
}
