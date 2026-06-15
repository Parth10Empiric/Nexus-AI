import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useAgentSocket } from "../hooks/useAgentSocket.js";

/**
 * VoiceAgentPanel — "Voice Agent Mode" toggle + live state visualizer (4.5).
 *
 * Reflects the Python session-orchestrator's real-time state over WebSocket:
 *   off | sleeping | listening | thinking | speaking
 * The toggle arms/disarms the agent; it shows optimistic feedback immediately
 * and reconciles with the backend's reported state.
 */

const STATE_META = {
  off:       { label: "Off",            color: "text-nexus-faint",  dot: "bg-nexus-faint",   pulse: false, icon: "○" },
  sleeping:  { label: "Sleeping",       color: "text-nexus-online", dot: "bg-nexus-online",  pulse: true,  icon: "💤" },
  listening: { label: "Listening",      color: "text-nexus-accent", dot: "bg-nexus-accent",  pulse: true,  icon: "🎧" },
  thinking:  { label: "Thinking",       color: "text-yellow-400",   dot: "bg-yellow-400",    pulse: true,  icon: "🧠" },
  speaking:  { label: "Speaking",       color: "text-nexus-accent", dot: "bg-nexus-accent",  pulse: true,  icon: "🔊" },
  connecting:{ label: "Connecting…",    color: "text-nexus-muted",  dot: "bg-nexus-muted",   pulse: true,  icon: "…" },
};

export default function VoiceAgentPanel() {
  const { connected, agentState, detail, activate, deactivate, interrupt } = useAgentSocket();
  const [pending, setPending] = useState(false);
  const busy = agentState === "thinking" || agentState === "speaking";

  // Clear the optimistic "pending" flag once the backend confirms a new state.
  useEffect(() => { setPending(false); }, [agentState]);

  // UI Phase 1 — if the orb is closed from its own right-click menu, the Rust
  // side emits `orb://closed`; tear the voice session down so the toggle and
  // the (now gone) widget never disagree.
  useEffect(() => {
    const unlisten = listen("orb://closed", () => deactivate());
    return () => { unlisten.then((off) => off()); };
  }, [deactivate]);

  const isOn = agentState !== "off";
  const view = pending ? "connecting" : agentState;
  const meta = STATE_META[view] || STATE_META.off;

  const handleToggle = () => {
    if (!connected) return;
    setPending(true);
    if (isOn) {
      deactivate();
      invoke("close_agent_orb").catch(() => {}); // destroy the floating widget
    } else {
      activate();
      invoke("spawn_agent_orb").catch(() => {}); // pop the floating widget
    }
  };

  return (
    <section className="nexus-card p-5">
      <header className="flex items-center justify-between mb-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-nexus-text">Voice Agent · Nexus</h2>
          <p className="text-[11px] mt-0.5 flex items-center gap-1.5">
            <span className={`inline-block h-1.5 w-1.5 rounded-full ${connected ? "bg-nexus-online" : "bg-nexus-offline"}`} />
            <span className={connected ? "text-nexus-faint" : "text-nexus-offline"}>
              {connected ? "backend connected" : "backend offline — start session_orchestrator"}
            </span>
          </p>
        </div>

        {/* Toggle switch */}
        <button
          onClick={handleToggle}
          disabled={!connected || pending}
          aria-pressed={isOn}
          title={isOn ? "Turn voice agent off" : "Turn voice agent on"}
          className={`relative h-8 w-14 flex-shrink-0 rounded-full transition-colors duration-200 ease-nexus disabled:opacity-40 disabled:cursor-not-allowed ${
            isOn ? "bg-nexus-accent" : "bg-nexus-elevated border border-nexus-border"
          }`}
        >
          <span
            className={`absolute top-1 h-6 w-6 rounded-full bg-white shadow transition-transform duration-200 ease-nexus ${
              isOn ? "translate-x-7" : "translate-x-1"
            }`}
          />
        </button>
      </header>

      {/* Live state visualizer */}
      <div className="flex items-center gap-3 rounded-lg bg-nexus-bg border border-nexus-border px-4 py-3">
        <span className="text-lg leading-none" aria-hidden="true">{meta.icon}</span>
        <span className="relative flex h-3 w-3">
          {meta.pulse && (
            <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 animate-pulse-slow ${meta.dot}`} />
          )}
          <span className={`relative inline-flex h-3 w-3 rounded-full ${meta.dot}`} />
        </span>
        <div className="min-w-0 flex-1">
          <div className={`text-sm font-semibold ${meta.color}`}>{meta.label}</div>
          <div className="text-[11px] text-nexus-faint truncate-line">
            {pending ? "talking to backend…" : detail || (isOn ? "say the wake word, then converse" : "agent is off")}
          </div>
        </div>
      </div>

      {/* Interrupt button — active while thinking/speaking (same as Shift+I). */}
      <button
        onClick={interrupt}
        disabled={!busy}
        className="mt-3 w-full h-9 rounded-lg text-sm font-medium border transition-colors duration-150 disabled:opacity-40 disabled:cursor-not-allowed bg-nexus-offline/15 text-nexus-offline border-nexus-offline/30 hover:bg-nexus-offline/25"
      >
        ✋ Interrupt (Shift+I)
      </button>

      <p className="mt-3 text-[11px] text-nexus-faint leading-relaxed">
        Say <b className="text-nexus-muted">“hey jarvis”</b> to start, talk one turn at a
        time (it listens, then answers, then listens again). Press
        <b className="text-nexus-muted"> Shift+I</b> to interrupt. Say
        <b className="text-nexus-muted">“bye”</b> (or toggle off) to turn the agent off.
      </p>
    </section>
  );
}
