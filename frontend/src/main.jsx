import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import AgentOrb from "./components/AgentOrb.jsx";
import "./index.css";

// UI Phase 1 — single bundle, two windows. The Rust side opens the floating
// widget at `index.html?window=orb`; everything else is the main dashboard.
// We tag <html> so the orb window's background can be forced transparent.
const isOrb = new URLSearchParams(window.location.search).get("window") === "orb";
if (isOrb) document.documentElement.classList.add("orb-window");

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>{isOrb ? <AgentOrb /> : <App />}</React.StrictMode>
);
