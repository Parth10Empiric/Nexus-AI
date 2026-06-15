import { useCallback, useEffect, useRef, useState } from "react";

/**
 * useAgentSocket — connects the React UI to the Python orchestrator's WebSocket
 * bridge (Phase 4.5 / 5.3).
 *
 * - Receives state events ("off" | "sleeping" | "listening" | "thinking" |
 *   "speaking") for the Voice Agent panel.
 * - Receives text-chat stream events ("user" | "token" | "answer") so the
 *   desktop chat can run through the SAME screen+files pipeline as voice.
 * - Lets the UI send commands: activate/deactivate (voice) and ask (text).
 *
 * Pass an optional `onEvent(msg)` to subscribe to every message; it's stored in
 * a ref so changing it never re-opens the socket.
 */
const WS_URL = "ws://127.0.0.1:8765";

export function useAgentSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const [agentState, setAgentState] = useState("off");
  const [detail, setDetail] = useState("");
  const wsRef = useRef(null);
  const retryRef = useRef(null);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      scheduleRetry();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onmessage = (evt) => {
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch {
        return;
      }
      if (msg.type === "state") {
        setAgentState(msg.state);
        setDetail(msg.detail || "");
      }
      if (onEventRef.current) onEventRef.current(msg);
    };
    ws.onclose = () => {
      setConnected(false);
      setAgentState("off");
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
      if (wsRef.current) wsRef.current.close();
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

  const activate = useCallback(() => sendObj({ cmd: "activate" }), [sendObj]);
  const deactivate = useCallback(() => sendObj({ cmd: "deactivate" }), [sendObj]);
  const ask = useCallback((text) => sendObj({ cmd: "ask", text }), [sendObj]);
  const interrupt = useCallback(() => sendObj({ cmd: "interrupt" }), [sendObj]);
  // UI Phase 1 — orb "Sleep" menu item: drop to standby without tearing the
  // session down (Python reports back state="sleeping").
  const sleep = useCallback(() => sendObj({ cmd: "sleep" }), [sendObj]);

  return { connected, agentState, detail, send: sendObj, activate, deactivate, ask, interrupt, sleep };
}
