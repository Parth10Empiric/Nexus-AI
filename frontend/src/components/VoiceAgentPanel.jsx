import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useNexusSocket } from "../context/NexusSocket.jsx";
import { useVoiceStream } from "../hooks/useVoiceStream.js";
import { useTtsPlayback } from "../hooks/useTtsPlayback.js";

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
  // Voice-OUT: play the server's synthesized speech. This is the ONLY component
  // that plays it (it's broadcast to all windows) so there's no double audio.
  const { playPcm, stop: stopSpeech, isPlaying, prime: primeAudio } = useTtsPlayback();
  const onSocketEvent = useCallback(
    (msg) => {
      if (msg.type === "tts_audio") playPcm(msg.pcm_b64, msg.sample_rate);
    },
    [playPcm]
  );

  const { connected, agentState, detail, activate, deactivate, interrupt, sendAudioChunk, endAudio } =
    useNexusSocket(onSocketEvent);

  // Turning the agent off must immediately silence any reply still playing.
  useEffect(() => {
    if (agentState === "off") stopSpeech();
  }, [agentState, stopSpeech]);
  const [pending, setPending] = useState(false);
  const busy = agentState === "thinking" || agentState === "speaking";

  // Echo guard: keep the mic OFF while a reply is playing, plus a short cooldown
  // after it ends, so the agent never hears (and re-asks) its own voice. Without
  // this the server flips to "listening" the moment it SENDS audio, while the
  // client is still PLAYING it → acoustic feedback loop.
  const [cooldown, setCooldown] = useState(false);
  useEffect(() => {
    if (isPlaying) {
      setCooldown(true);
      return undefined;
    }
    const t = setTimeout(() => setCooldown(false), 600); // let the room/tail settle
    return () => clearTimeout(t);
  }, [isPlaying]);

  // Voice-in: stream the mic to the server only when truly listening AND not
  // speaking. (No wake word — arming the toggle starts listening; ~1s silence
  // ends a turn.)
  const micActive = agentState === "listening" && !isPlaying && !cooldown;
  const { micError } = useVoiceStream(micActive, { sendAudioChunk, endAudio });

  // Clear the optimistic "pending" flag once the backend confirms a new state.
  useEffect(() => { setPending(false); }, [agentState]);

  // Safety net: never let "connecting…" get stuck. If the backend doesn't
  // confirm a state change within 4s (or sends the same state), clear it so the
  // toggle stays usable. Also clears if the socket drops.
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => setPending(false), 4000);
    return () => clearTimeout(t);
  }, [pending]);
  useEffect(() => { if (!connected) setPending(false); }, [connected]);

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

  // Interrupt = silence the current spoken reply immediately AND tell the server
  // to stop, so the user regains the floor without waiting for it to finish.
  const handleInterrupt = () => {
    stopSpeech();
    interrupt();
  };

  const handleToggle = () => {
    if (!connected) return;
    primeAudio(); // unlock audio on this click so replies can play
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
              {connected ? "backend connected" : "backend offline — is the Nexus server running?"}
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
            className={`absolute top-1 h-6 w-6 -ml-6 rounded-full bg-white shadow transition-transform duration-200 ease-nexus ${
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
          <div className={`text-[11px] truncate-line ${micError ? "text-nexus-offline" : "text-nexus-faint"}`}>
            {micError
              ? `mic: ${micError}`
              : pending
              ? "talking to backend…"
              : detail || (isOn ? "listening — just speak (no wake word needed)" : "agent is off")}
          </div>
        </div>
      </div>

      {/* Interrupt button — active while thinking/speaking (same as Shift+I). */}
      <button
        onClick={handleInterrupt}
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
