import { useRef } from "react";
import { useActiveWindow } from "./hooks/useActiveWindow.js";
import StatusIndicator from "./components/StatusIndicator.jsx";
import ActiveWindowCard from "./components/ActiveWindowCard.jsx";
import ChatPanel from "./components/ChatPanel.jsx";
import VoiceAgentPanel from "./components/VoiceAgentPanel.jsx";

import { useAudioMic } from "./hooks/useAudioMic.js";

/**
 * App — the Nexus AI dashboard shell.
 *
 * It owns the single data stream (useActiveWindow) and feeds it to BOTH:
 *   - the left column (ActiveWindowCard + privacy note), and
 *   - the ChatPanel, whose every question is silently augmented with the
 *     current active-window context.
 *
 * One source of truth → the card and the AI always agree on what you're doing.
 */
export default function App() {
  const { entry, status } = useActiveWindow();

  // ── TEMP: Phase 6.2 mic-capture probe (remove after testing) ──────────────
  const seen = useRef({ chunks: 0, samples: 0 });
  const { startRecording, stopRecording, isRecording, error } = useAudioMic(
    (chunk) => {
      seen.current.chunks += 1;
      seen.current.samples += chunk.length;
      console.log("[mic chunk]", chunk.length, "samples — total", seen.current);
    }
  );
  // ──────────────────────────────────────────────────────────────────────────

  // The context object handed to the AI on every message.
  const aiContext = entry
    ? { appName: entry.appName, title: entry.title }
    : null;

  return (
    <div className="h-full w-full flex flex-col bg-nexus-bg">
      {/* ---- Top bar ---- */}
      <header className="flex items-center justify-between px-8 h-16 border-b border-nexus-border bg-nexus-bg/80 backdrop-blur flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="grid place-items-center h-8 w-8 rounded-md bg-nexus-accent/15 text-nexus-accent font-bold">
            N
          </div>
          <div className="leading-tight">
            <h1 className="text-sm font-semibold text-nexus-text">Nexus AI</h1>
            <p className="text-[11px] text-nexus-faint">
              Local Observer · Expert Brain
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* TEMP: Phase 6.2 mic-capture probe (remove after testing) */}
          <button
            onClick={isRecording ? stopRecording : startRecording}
            className="text-xs px-3 py-1.5 rounded-md border border-nexus-border text-nexus-text hover:bg-nexus-elevated"
          >
            {error ?? (isRecording ? "Stop mic" : "Start mic")}
          </button>
          <StatusIndicator status={status} />
        </div>
      </header>

      {/* ---- Main content: context column + chat ---- */}
      <main className="flex-1 min-h-0 px-6 py-6">
        <div className="mx-auto w-full max-w-6xl h-full grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
          {/* Left: live context */}
          <div className="flex flex-col gap-4 min-h-0 animate-fadein">
            <VoiceAgentPanel />
            <ActiveWindowCard entry={entry} />
            <div className="nexus-card p-4 flex items-start gap-3">
              <div className="grid place-items-center h-7 w-7 rounded-md bg-nexus-elevated text-nexus-online flex-shrink-0">
                ✓
              </div>
              <p className="text-xs text-nexus-muted leading-relaxed">
                <span className="text-nexus-text font-medium">
                  Private by design.
                </span>{" "}
                The AI runs locally via Ollama. Your active window is shared with
                it automatically — nothing leaves this machine.
              </p>
            </div>
          </div>

          {/* Right: AI chat (gets the live context) */}
          <div className="min-h-0 animate-fadein">
            <ChatPanel context={aiContext} />
          </div>
        </div>
      </main>

      {/* ---- Footer ---- */}
      <footer className="px-8 h-9 flex items-center border-t border-nexus-border text-[11px] text-nexus-faint flex-shrink-0">
        Phase 2.1 · Local LLM (Ollama) · Tauri + React
      </footer>
    </div>
  );
}
