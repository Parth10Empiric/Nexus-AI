import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

/**
 * NexusSocket — ONE shared WebSocket per window (Phase 7.3 fix).
 *
 * Previously every component (chat, voice panel, orb) called useAgentSocket()
 * and opened its OWN socket, so a single client showed up as many connections
 * (worsened by dev hot-reloads). Now a single provider owns one socket and all
 * components consume it via `useNexusSocket()`. Each browser window mounts one
 * provider → exactly one connection (the orb is a separate window, so it has
 * its own single socket — that's expected).
 *
 * Modes (env-driven, unchanged):
 *   • LOCAL — no invite key → ws://127.0.0.1:8765, connected on open.
 *   • SaaS  — VITE_NEXUS_INVITE_KEY set → VITE_NEXUS_WS_URL, sends the auth
 *             handshake first; `connected` only after `auth_ok`.
 *
 * Components subscribe to raw messages with `subscribe(fn)` (returns an
 * unsubscribe). State events update `agentState`/`detail` for everyone.
 */
const WS_URL = import.meta.env.VITE_NEXUS_WS_URL || "ws://127.0.0.1:8765";
const INVITE_KEY = import.meta.env.VITE_NEXUS_INVITE_KEY || "";
const SAAS_MODE = INVITE_KEY.length > 0;

const NexusSocketContext = createContext(null);

export function NexusSocketProvider({ children }) {
  const [connected, setConnected] = useState(false);
  const [agentState, setAgentState] = useState("off");
  const [detail, setDetail] = useState("");

  const wsRef = useRef(null);
  const retryRef = useRef(null);
  const listenersRef = useRef(new Set());

  // subscribe(fn) → unsubscribe(). Fn is called with every parsed message.
  const subscribe = useCallback((fn) => {
    listenersRef.current.add(fn);
    return () => listenersRef.current.delete(fn);
  }, []);

  const connect = useCallback(() => {
    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      scheduleRetry();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      if (SAAS_MODE) {
        ws.send(JSON.stringify({ type: "auth", invite_key: INVITE_KEY }));
        setDetail("authenticating…");
      } else {
        setConnected(true);
      }
    };
    ws.onmessage = (evt) => {
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch {
        return;
      }
      if (msg.type === "auth_ok") {
        setConnected(true);
        setDetail("");
      }
      if (msg.type === "state") {
        setAgentState(msg.state);
        setDetail(msg.detail || "");
      }
      listenersRef.current.forEach((fn) => {
        try {
          fn(msg);
        } catch {
          /* a bad listener must not break delivery to the others */
        }
      });
    };
    ws.onclose = (evt) => {
      setConnected(false);
      setAgentState("off");
      if (evt && evt.code === 1008) setDetail(evt.reason || "authentication failed");
      scheduleRetry();
    };
    ws.onerror = () => ws.close();

    function scheduleRetry() {
      if (retryRef.current) return;
      retryRef.current = setTimeout(() => {
        retryRef.current = null;
        connect();
      }, 1500);
    }
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (retryRef.current) clearTimeout(retryRef.current);
      const ws = wsRef.current;
      wsRef.current = null;
      // Clean close so the server drops this socket immediately (no ghosts).
      if (ws) {
        ws.onclose = null; // don't trigger a reconnect on intentional teardown
        ws.close();
      }
    };
  }, [connect]);

  const sendObj = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }, []);

  const sendBinary = useCallback((arrayBuffer) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(arrayBuffer);
      return true;
    }
    return false;
  }, []);

  const value = {
    connected,
    agentState,
    detail,
    subscribe,
    send: sendObj,
    activate: useCallback(() => sendObj({ cmd: "activate" }), [sendObj]),
    deactivate: useCallback(() => sendObj({ cmd: "deactivate" }), [sendObj]),
    ask: useCallback((text) => sendObj({ cmd: "ask", text }), [sendObj]),
    interrupt: useCallback(() => sendObj({ cmd: "interrupt" }), [sendObj]),
    sleep: useCallback(() => sendObj({ cmd: "sleep" }), [sendObj]),
    syncFile: useCallback(
      (filePath, fileContent) =>
        sendObj({ type: "file_sync", file_path: filePath, file_content: fileContent }),
      [sendObj]
    ),
    sendAudioChunk: useCallback((int16) => sendBinary(int16.buffer), [sendBinary]),
    endAudio: useCallback(() => sendObj({ type: "audio_end" }), [sendObj]),
  };

  return <NexusSocketContext.Provider value={value}>{children}</NexusSocketContext.Provider>;
}

/**
 * useNexusSocket(onEvent?) — consume the shared socket. If `onEvent` is given,
 * it's subscribed for the component's lifetime (replaces the old per-component
 * useAgentSocket(onEvent) ergonomics).
 */
export function useNexusSocket(onEvent) {
  const ctx = useContext(NexusSocketContext);
  if (!ctx) throw new Error("useNexusSocket must be used within <NexusSocketProvider>");

  const { subscribe } = ctx;
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;

  useEffect(() => {
    if (!cbRef.current) return undefined;
    return subscribe((msg) => cbRef.current && cbRef.current(msg));
  }, [subscribe]);

  return ctx;
}
