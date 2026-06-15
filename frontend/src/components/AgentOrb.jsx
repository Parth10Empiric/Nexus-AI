import { useCallback, useEffect, useRef, useState } from "react";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { invoke } from "@tauri-apps/api/core";
import { useAgentSocket } from "../hooks/useAgentSocket.js";

/**
 * AgentOrb — UI Phase 1 · the Floating Agent Widget.
 *
 * Lives in its OWN transparent, undecorated, always-on-top Tauri window
 * (spawned by the Rust `spawn_agent_orb` command, routed via `?window=orb`).
 * It is a single glassy orb that floats over VS Code / the browser while the
 * user codes.
 *
 * State is NOT pushed from the dashboard — the orb opens its own WebSocket to
 * the Python orchestrator (`useAgentSocket`) and reacts to the exact same
 * broadcast the dashboard sees, so the two windows are always in lock-step
 * (see the IPC notes in docs/UI_Phase_1.md).
 *
 * Interactions:
 *   • Drag        — press + move the orb → native OS window drag.
 *   • Double-click — toggle the mic listener (bypasses the wake word).
 *   • Right-click  — custom Tailwind menu: Sleep / Close Agent.
 */

// Per-state visuals. `ring` drives the colored glow; the JSX picks the
// animation layer (halo / orbit / waveform) from `view`.
const STATES = {
  off: { label: "Off", ring: "#6b6b6b", core: "from-nexus-faint/40 to-nexus-faint/10" },
  connecting: { label: "Connecting…", ring: "#6b6b6b", core: "from-nexus-muted/40 to-nexus-muted/10" },
  sleeping: { label: "Standby", ring: "#3ddc84", core: "from-nexus-online/40 to-nexus-online/5" },
  listening: { label: "Listening", ring: "#4f9dff", core: "from-nexus-accent/60 to-nexus-accent/10" },
  thinking: { label: "Thinking", ring: "#fbbf24", core: "from-yellow-400/60 to-yellow-400/10" },
  speaking: { label: "Speaking", ring: "#4f9dff", core: "from-nexus-accent/60 to-nexus-online/15" },
};

const DRAG_THRESHOLD = 4; // px before a press becomes a window drag

export default function AgentOrb() {
  const { connected, agentState, detail, activate, deactivate, sleep } = useAgentSocket();
  const [menu, setMenu] = useState(null); // {x, y} | null
  const pressRef = useRef(null); // {x, y, dragging} | null
  const appWindow = useRef(getCurrentWebviewWindow());

  const view = connected ? agentState : "connecting";
  const meta = STATES[view] || STATES.off;
  const isListening = agentState === "listening";

  // ---- Drag: start the OS-level move only after the pointer actually moves,
  // so a stationary double-click / right-click is never swallowed by a drag.
  const onPointerDown = useCallback((e) => {
    if (e.button !== 0) return; // left button only
    pressRef.current = { x: e.clientX, y: e.clientY, dragging: false };
  }, []);

  const onPointerMove = useCallback((e) => {
    const p = pressRef.current;
    if (!p || p.dragging) return;
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) > DRAG_THRESHOLD) {
      p.dragging = true;
      appWindow.current.startDragging(); // hands control to the window manager
    }
  }, []);

  const endPress = useCallback(() => {
    pressRef.current = null;
  }, []);

  // ---- Double-click: manual mic toggle (bypass wake word).
  const onDoubleClick = useCallback(() => {
    if (!connected) return;
    isListening ? deactivate() : activate();
  }, [connected, isListening, activate, deactivate]);

  // ---- Right-click: open the custom menu at the cursor.
  const onContextMenu = useCallback((e) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY });
  }, []);

  // Close the menu on any outside click or Escape.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e) => e.key === "Escape" && setMenu(null);
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  const handleSleep = useCallback(() => {
    setMenu(null);
    sleep();
  }, [sleep]);

  // "Close Agent" → ask Rust to destroy this window. The dashboard hears
  // `orb://closed` and flips its toggle back off.
  const handleClose = useCallback(() => {
    setMenu(null);
    invoke("close_agent_orb").catch(() => {});
  }, []);

  return (
    <div className="h-screen w-screen grid place-items-center overflow-hidden select-none">
      {/* ---- The orb -------------------------------------------------------- */}
      <button
        type="button"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endPress}
        onPointerLeave={endPress}
        onDoubleClick={onDoubleClick}
        onContextMenu={onContextMenu}
        title={`Nexus · ${meta.label} — double-click to toggle mic, right-click for options`}
        className="relative grid place-items-center h-28 w-28 rounded-full cursor-grab active:cursor-grabbing focus:outline-none"
        style={{ WebkitAppRegion: "no-drag" }}
      >
        {/* Expanding halo — only while Listening. */}
        {view === "listening" && (
          <>
            <span className="absolute h-24 w-24 rounded-full animate-halo" style={{ background: `${meta.ring}55` }} />
            <span className="absolute h-24 w-24 rounded-full animate-halo [animation-delay:0.9s]" style={{ background: `${meta.ring}40` }} />
          </>
        )}

        {/* Soft ambient glow that tints to the current state. */}
        <span
          className="absolute h-28 w-28 rounded-full blur-xl opacity-70 transition-colors duration-500"
          style={{ background: `radial-gradient(circle, ${meta.ring}88 0%, transparent 70%)` }}
        />

        {/* Rotating conic ring — only while Thinking. */}
        {view === "thinking" && (
          <span
            className="absolute h-24 w-24 rounded-full animate-orbit"
            style={{ background: `conic-gradient(from 0deg, transparent 0deg, ${meta.ring} 90deg, transparent 200deg)`, mask: "radial-gradient(farthest-side, transparent calc(100% - 4px), #000 calc(100% - 4px))", WebkitMask: "radial-gradient(farthest-side, transparent calc(100% - 4px), #000 calc(100% - 4px))" }}
          />
        )}

        {/* Glassy core. Breathes when idle/standby; gently lit otherwise. */}
        <span
          className={`relative grid place-items-center h-20 w-20 rounded-full bg-gradient-to-br ${meta.core} border border-white/10 backdrop-blur-md shadow-[inset_0_1px_1px_rgba(255,255,255,0.25),0_8px_24px_rgba(0,0,0,0.45)] transition-all duration-500 ${
            view === "sleeping" || view === "off" || view === "connecting" ? "animate-breathe" : ""
          }`}
          style={{ boxShadow: `inset 0 1px 1px rgba(255,255,255,0.25), 0 0 28px ${meta.ring}66` }}
        >
          {/* Speaking waveform — five bars bouncing inside the core. */}
          {view === "speaking" ? (
            <span className="flex items-end gap-[3px] h-7">
              {[0, 1, 2, 3, 4].map((i) => (
                <span
                  key={i}
                  className="w-[3px] rounded-full bg-white/90 animate-wave"
                  style={{ height: "100%", animationDelay: `${i * 0.12}s` }}
                />
              ))}
            </span>
          ) : (
            // Idle/listening/thinking → the Nexus "N" mark.
            <span className="text-2xl font-bold text-white/90 drop-shadow">N</span>
          )}
        </span>

        {/* Tiny connection dot so the user knows the backend is reachable. */}
        <span
          className={`absolute bottom-1 right-1 h-2.5 w-2.5 rounded-full border-2 border-black/40 ${
            connected ? "bg-nexus-online" : "bg-nexus-offline animate-pulse-slow"
          }`}
        />
      </button>

      {/* ---- Custom right-click menu --------------------------------------- */}
      {menu && (
        <div
          // Stop the window-level pointerdown listener from closing it instantly.
          onPointerDown={(e) => e.stopPropagation()}
          className="fixed z-50 w-44 rounded-xl border border-nexus-border bg-nexus-surface/95 backdrop-blur-md shadow-card p-1 animate-fadein"
          style={{ left: Math.min(menu.x, window.innerWidth - 180), top: Math.min(menu.y, window.innerHeight - 110) }}
        >
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-nexus-faint">
            Nexus · {meta.label}
          </div>
          <button
            onClick={handleSleep}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-nexus-text hover:bg-nexus-elevated transition-colors duration-150"
          >
            <span className="text-base leading-none">🌙</span> Sleep
          </button>
          <button
            onClick={handleClose}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm text-nexus-offline hover:bg-nexus-offline/15 transition-colors duration-150"
          >
            <span className="text-base leading-none">⏻</span> Close Agent
          </button>
        </div>
      )}

      {/* Optional status caption under the orb (kept subtle). */}
      {detail && (
        <span className="absolute bottom-1 text-[10px] text-nexus-muted/80 max-w-[120px] truncate-line text-center">
          {detail}
        </span>
      )}
    </div>
  );
}
