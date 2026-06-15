# UI Phase 1 — Floating Agent Widget (the "Nexus Orb")

> Premium, always-on-top floating agent orb for Nexus AI.
> **Stack:** Tauri 2 (Rust) · React 18 · Tailwind 3 · Python orchestrator (WebSocket).
> **Status:** Implemented & verified — `cargo check` ✅ and `vite build` ✅ both pass.

When the user flips **Start Agent** in the main dashboard, the Rust backend spawns a
*second* window: a borderless, transparent, always-on-top orb that floats over VS Code /
the browser while they code. It pulses when listening, spins when thinking, shows a
waveform when speaking, is dragged anywhere on screen, double-clicked to toggle the mic,
and right-clicked for a glassy **Sleep / Close Agent** menu.

## Files touched

| File | Change |
| --- | --- |
| `frontend/src-tauri/src/main.rs` | `spawn_agent_orb` / `close_agent_orb` commands + `orb://` lifecycle events |
| `frontend/src-tauri/tauri.conf.json` | `app.macOSPrivateApi: true` (transparency) |
| `frontend/src-tauri/Cargo.toml` | `tauri` feature `macos-private-api` |
| `frontend/src-tauri/capabilities/agent-orb.json` | ACL grants: window drag + event bus |
| `frontend/src/main.jsx` | routes `?window=orb` → `<AgentOrb />` |
| `frontend/src/components/AgentOrb.jsx` | **the orb** — drag, dbl-click, right-click menu, visual states |
| `frontend/src/components/VoiceAgentPanel.jsx` | toggle now spawns/destroys the orb |
| `frontend/src/hooks/useAgentSocket.js` | added `sleep` command + raw `send` |
| `frontend/src/index.css` | `.orb-window` transparent background |
| `frontend/tailwind.config.js` | `breathe` / `halo` / `orbit` / `wave` keyframes |

---

## 1. TAURI RUST & CONFIG

### `tauri.conf.json`

Transparency needs the private-API flag (the main window stays as-is — the orb is created
**at runtime**, not declared here, so two orbs can never stack):

```jsonc
"app": {
  "macOSPrivateApi": true,        // <-- added; required for transparent windows on macOS
  "windows": [ /* ...existing main window unchanged... */ ]
}
```

### `Cargo.toml`

`macOSPrivateApi` in the config must be matched by the crate feature or the build fails:

```toml
tauri = { version = "2", features = ["macos-private-api"] }
```

### `capabilities/agent-orb.json` (new)

The project had **no** capabilities yet (custom commands aren't ACL-gated, so the existing
`invoke('get_active_window')` worked). But the orb's JS calls **core** APIs —
`startDragging()` and the event bus — which *are* gated. This grants them:

```jsonc
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "agent-orb",
  "windows": ["main", "agent-orb"],
  "permissions": [
    "core:event:default",
    "core:window:allow-start-dragging",
    "core:window:allow-set-always-on-top",
    "core:window:allow-set-position",
    "core:window:allow-set-focus",
    "core:window:allow-show",
    "core:window:allow-hide",
    "core:window:allow-close"
  ]
}
```

### `main.rs` — spawn the transparent, undecorated, always-on-top window

```rust
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

const ORB_LABEL: &str = "agent-orb";

#[tauri::command]
fn spawn_agent_orb(app: AppHandle) -> Result<(), String> {
    // Re-show instead of stacking a second orb.
    if let Some(existing) = app.get_webview_window(ORB_LABEL) {
        let _ = existing.show();
        let _ = existing.set_focus();
        let _ = app.emit("orb://opened", ());
        return Ok(());
    }

    WebviewWindowBuilder::new(&app, ORB_LABEL, WebviewUrl::App("index.html?window=orb".into()))
        .title("Nexus Agent")
        .inner_size(168.0, 168.0)
        .decorations(false)   // no title bar / chrome
        .transparent(true)    // desktop shows through the empty corners
        .always_on_top(true)  // never hides behind VS Code / browser
        .skip_taskbar(true)   // overlay, not an entry in the switcher
        .resizable(false)
        .shadow(false)        // we draw our own glow in CSS
        .focused(true)
        .position(1200.0, 120.0)
        .build()
        .map_err(|e| e.to_string())?;

    let _ = app.emit("orb://opened", ());
    Ok(())
}

#[tauri::command]
fn close_agent_orb(app: AppHandle) -> Result<(), String> {
    if let Some(orb) = app.get_webview_window(ORB_LABEL) {
        orb.close().map_err(|e| e.to_string())?;
    }
    let _ = app.emit("orb://closed", ()); // dashboard converges to "off"
    Ok(())
}

// register both in the builder:
.invoke_handler(tauri::generate_handler![
    get_active_window,
    get_active_file_context,
    spawn_agent_orb,
    close_agent_orb
])
```

**Why one bundle, two windows?** Both the dashboard and the orb load the same Vite build.
The orb is just `index.html?window=orb`; `main.jsx` reads that query string and renders
`<AgentOrb />` instead of `<App />`. No second build target, no duplicate config.

> **Click-through note:** The window is sized tight to the orb (168×168) so the only
> non-orb pixels are the four small transparent corners. For *true* per-pixel pass-through
> of those corners, add `core:window:allow-set-ignore-cursor-events` to the capability and
> call `appWindow.setIgnoreCursorEvents(true/false)` on the orb's pointer enter/leave.

---

## 2. REACT WIDGET CODE — `AgentOrb.jsx`

Full component lives at
[frontend/src/components/AgentOrb.jsx](../frontend/src/components/AgentOrb.jsx). Key logic:

**Drag vs. click disambiguation.** `startDragging()` immediately hands control to the
window manager and would swallow a click. So we only start the OS drag *after* the pointer
moves past a 4px threshold — a stationary double-click or right-click is never eaten:

```jsx
const onPointerDown = (e) => { if (e.button === 0) pressRef.current = { x: e.clientX, y: e.clientY, dragging: false }; };
const onPointerMove = (e) => {
  const p = pressRef.current;
  if (!p || p.dragging) return;
  if (Math.hypot(e.clientX - p.x, e.clientY - p.y) > 4) {
    p.dragging = true;
    appWindow.current.startDragging();   // native, screen-wide window move
  }
};
```

**Double-click → manual mic toggle (bypasses wake word):**

```jsx
const onDoubleClick = () => { if (connected) isListening ? deactivate() : activate(); };
```

**Right-click → custom Tailwind menu (Sleep / Close Agent):** positioned at the cursor,
clamped to the window, closes on outside-click/Escape. *Sleep* sends `{cmd:"sleep"}` over
the WebSocket; *Close Agent* calls `invoke("close_agent_orb")` to destroy the window.

**Visual states** (all pure Tailwind keyframes — no Framer Motion dependency added):

| State | Look |
| --- | --- |
| `listening` | blue core + expanding **halo** rings (`animate-halo`) |
| `thinking` | amber **rotating conic ring** (`animate-orbit`) |
| `speaking` | 5-bar **waveform** inside the core (`animate-wave`) |
| `sleeping` / `off` | dim slow **breathe** (`animate-breathe`) |

The orb is glassy via `bg-gradient-to-br`, `backdrop-blur-md`, an inset highlight, and a
state-tinted `box-shadow` glow — all using the existing `nexus-*` design tokens.

---

## 3. IPC EXPLANATION — keeping every window in sync

Three channels, each with one job:

**A) Listening-state — Python ⇄ all windows (WebSocket, single source of truth).**
The orb does *not* receive state pushed from the dashboard. Instead it opens its **own**
`useAgentSocket()` connection to the Python orchestrator at `ws://127.0.0.1:8765` — the
exact same socket the dashboard uses. Python broadcasts `{type:"state", state:"listening"}`
to every connected client, so the dashboard panel and the orb light up *simultaneously*
from one broadcast. Because state flows from Python (not window→window), the two windows
can never drift. When the **Python wake-word engine** hears "Hey Nexus" / "Hey Jarvis", it
just flips its state and broadcasts — the orb starts pulsing with zero Tauri code involved.
Commands flow back the same way: double-click sends `{cmd:"activate"}`, the Sleep menu
sends `{cmd:"sleep"}`.

**B) Window lifecycle — dashboard ⇄ orb (Tauri events).**
The WebSocket carries *state*, but window *existence* is a Tauri concern. The dashboard
toggle calls `invoke("spawn_agent_orb")` / `invoke("close_agent_orb")`. Rust emits
`orb://opened` and `orb://closed` to all windows. The dashboard `listen("orb://closed", …)`
so that if the user picks **Close Agent** from the orb's own menu, the dashboard toggle
flips back to off — the toggle and the (now-destroyed) widget always agree.

**C) Window management — JS → Rust (Tauri commands).**
Spawn, destroy, and `startDragging()` are all native operations the Rust/WM layer owns.

```
 ┌──────────────┐   ws://…:8765 (state + cmds)   ┌──────────────────┐
 │  Python      │◀──────────────────────────────▶│  Main Dashboard  │
 │ orchestrator │◀───────────────┐               │ (VoiceAgentPanel)│
 │ + wake word  │   ws://…:8765  │               └────────┬─────────┘
 └──────────────┘                │                invoke spawn/close │  Tauri events
                                 ▼                                   ▼  orb://opened|closed
                         ┌───────────────┐   invoke close_agent_orb  ┌──────────────┐
                         │  Agent Orb    │──────────────────────────▶│  Rust core   │
                         │  (window 2)   │◀──────────────────────────│ (main.rs)    │
                         └───────────────┘     WebviewWindowBuilder   └──────────────┘
```

---

## Verify it

```bash
cd "frontend"
npm run tauri:dev          # launches the dashboard
# In the dashboard: flip "Voice Agent · Nexus" ON  →  the orb appears top-right.
#   • drag it anywhere          • double-click → Listening (blue pulse)
#   • right-click → Sleep / Close Agent
# Start the Python orchestrator so the orb reflects real listening/thinking/speaking states.
```

Build checks (already passing): `cargo check` in `src-tauri`, `npm run build` in `frontend`.
