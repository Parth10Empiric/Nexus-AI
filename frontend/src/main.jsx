import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import AgentOrb from "./components/AgentOrb.jsx";
import { NexusSocketProvider } from "./context/NexusSocket.jsx";
import "./index.css";

// UI Phase 1 — single bundle, two windows. The Rust side opens the floating
// widget at `index.html?window=orb`; everything else is the main dashboard.
// We tag <html> so the orb window's background can be forced transparent.
const isOrb = new URLSearchParams(window.location.search).get("window") === "orb";
if (isOrb) document.documentElement.classList.add("orb-window");

// One NexusSocketProvider per window → exactly ONE WebSocket per window; all
// components (chat, voice panel, orb) share it via useNexusSocket().
// NOTE: intentionally NOT wrapped in <React.StrictMode> — Strict Mode
// double-invokes effects in dev, which would open/close the socket twice and
// register the client as two connections.
ReactDOM.createRoot(document.getElementById("root")).render(
  <NexusSocketProvider>{isOrb ? <AgentOrb /> : <App />}</NexusSocketProvider>
);
