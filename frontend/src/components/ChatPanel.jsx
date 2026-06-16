import { useCallback, useEffect, useRef, useState } from "react";
import { streamGenerate } from "../lib/ollama.js";
import { useNexusSocket } from "../context/NexusSocket.jsx";

/**
 * ChatPanel — the conversation UI with the local AI.
 *
 * Props:
 *   context -> { appName, title } | null   (the live active-window from Phase 1)
 *
 * Behaviour:
 *   - Renders a scrollable history, a multiline input, and a send/stop button.
 *   - On send, it streams the model's reply via streamGenerate(), appending
 *     tokens to the last assistant message so the text types out live.
 *   - The active-window context is injected into every request invisibly.
 *   - Streaming runs off the render path (async + setState), so the UI never
 *     freezes while the model is generating.
 */

function Bubble({ role, content, streaming }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words ${
          isUser
            ? "bg-nexus-accent/15 text-nexus-text border border-nexus-accent/30"
            : "bg-nexus-elevated text-nexus-text border border-nexus-border"
        }`}
      >
        {content}
        {streaming && (
          <span className="inline-block w-1.5 h-4 ml-0.5 align-middle bg-nexus-accent animate-pulse-slow" />
        )}
      </div>
    </div>
  );
}

export default function ChatPanel({ context }) {
  const [messages, setMessages] = useState([]); // {role, content}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const scrollRef = useRef(null);
  const abortRef = useRef(null);
  // True right after WE typed+sent, so we ignore the server's echo of our own
  // {type:"user"} (we already rendered it). Voice questions have no such flag,
  // so they DO get rendered from the echo.
  const typedEchoPendingRef = useRef(false);

  // Backend WebSocket: typed AND spoken questions run through the SAME server
  // pipeline. We render voice turns here too (user line + streamed answer), so
  // the chat shows the conversation even when you only used the mic.
  const handleWsEvent = useCallback((msg) => {
    if (msg.type === "user") {
      // Our own typed echo → already shown; skip. Voice → render it.
      if (typedEchoPendingRef.current) {
        typedEchoPendingRef.current = false;
        return;
      }
      setMessages((prev) => [
        ...prev,
        { role: "user", content: msg.text || "" },
        { role: "assistant", content: "" },
      ]);
    } else if (msg.type === "token") {
      setMessages((prev) => {
        const next = prev.slice();
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next[next.length - 1] = { role: "assistant", content: (last.content || "") + msg.token };
        }
        return next;
      });
    } else if (msg.type === "answer") {
      // Authoritative full text (covers any dropped tokens).
      setMessages((prev) => {
        const next = prev.slice();
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next[next.length - 1] = { role: "assistant", content: msg.text || last.content };
        }
        return next;
      });
      setBusy(false);
    }
  }, []);
  const { connected: backendUp, ask } = useNexusSocket(handleWsEvent);

  // Keep the view pinned to the newest message as content streams in.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const stop = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
  }, []);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || busy) return;

    setInput("");
    setBusy(true);

    // Push the user message AND an empty assistant placeholder we'll fill.
    setMessages((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "" },
    ]);

    // Preferred path: route through the backend (screen + files + RAG), exactly
    // like the voice assistant. Tokens arrive via handleWsEvent.
    if (backendUp) {
      typedEchoPendingRef.current = true; // ignore the server's echo of this
      ask(question);
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamGenerate({
        prompt: question,
        context,
        signal: controller.signal,
        onToken: (_token, full) => {
          // Replace the last (assistant) message's content with the running total.
          setMessages((prev) => {
            const next = prev.slice();
            next[next.length - 1] = { role: "assistant", content: full };
            return next;
          });
        },
      });
    } catch (err) {
      const msg =
        err?.name === "AbortError"
          ? "_(stopped)_"
          : `⚠️ Could not reach the local AI. Is Ollama running? (${err.message})`;
      setMessages((prev) => {
        const next = prev.slice();
        const last = next[next.length - 1];
        next[next.length - 1] = {
          role: "assistant",
          content: (last?.content || "") + (last?.content ? "\n\n" : "") + msg,
        };
        return next;
      });
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }, [input, busy, context, backendUp, ask]);

  const onKeyDown = (e) => {
    // Enter sends, Shift+Enter makes a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <section className="nexus-card flex flex-col h-full overflow-hidden">
      {/* Header */}
      <header className="flex items-center justify-between px-5 h-14 border-b border-nexus-border flex-shrink-0">
        <div className="flex items-center gap-2.5">
          <span className="grid place-items-center h-7 w-7 rounded-md bg-nexus-accent/15 text-nexus-accent text-sm font-bold">
            AI
          </span>
          <div className="leading-tight">
            <h2 className="text-sm font-semibold text-nexus-text">Assistant</h2>
            <p className="text-[11px] text-nexus-faint font-mono">
              {backendUp ? "Nexus · screen + files" : "qwen2.5-coder:1.5b · local"}
            </p>
          </div>
        </div>
        {context?.title && (
          <span
            className="hidden sm:flex items-center gap-1.5 text-[11px] text-nexus-faint max-w-[45%]"
            title={context.title}
          >
            <span className="h-1.5 w-1.5 rounded-full bg-nexus-online flex-shrink-0" />
            <span className="truncate-line">ctx: {context.title}</span>
          </span>
        )}
      </header>

      {/* History */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-5 py-4 space-y-3"
      >
        {messages.length === 0 ? (
          <div className="h-full grid place-items-center text-center">
            <div className="max-w-sm">
              <p className="text-nexus-muted text-sm">
                Ask anything about your code.
              </p>
              <p className="text-nexus-faint text-xs mt-2">
                Your current window is shared automatically — no copy-pasting
                needed. Everything runs locally.
              </p>
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <Bubble
              key={i}
              role={m.role}
              content={m.content || (m.role === "assistant" ? "…" : "")}
              streaming={busy && i === messages.length - 1 && m.role === "assistant"}
            />
          ))
        )}
      </div>

      {/* Composer */}
      <div className="border-t border-nexus-border p-3 flex-shrink-0">
        <div className="flex items-end gap-2 bg-nexus-bg rounded-xl border border-nexus-border focus-within:border-nexus-accent/60 transition-colors duration-200 ease-nexus p-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder="Ask the assistant…  (Enter to send, Shift+Enter for newline)"
            className="flex-1 resize-none bg-transparent outline-none text-sm text-nexus-text placeholder:text-nexus-faint max-h-32 px-2 py-1.5"
          />
          {busy ? (
            <button
              onClick={stop}
              className="flex-shrink-0 h-9 px-4 rounded-lg text-sm font-medium bg-nexus-offline/15 text-nexus-offline border border-nexus-offline/30 hover:bg-nexus-offline/25 transition-colors duration-150"
            >
              Stop
            </button>
          ) : (
            <button
              onClick={send}
              disabled={!input.trim()}
              className="flex-shrink-0 h-9 px-4 rounded-lg text-sm font-medium bg-nexus-accent text-nexus-bg disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-110 transition-all duration-150"
            >
              Send
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
