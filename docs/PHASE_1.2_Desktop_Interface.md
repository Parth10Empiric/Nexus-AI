# Nexus AI — Phase 1.2: Tauri/React Desktop Interface (Enterprise Dark Mode)

> A learning guide. This document explains, in plain language, **what** we
> built in Phase 1.2, **why** we chose each tool, and **how** the code works
> under the hood — so you can read the codebase with full understanding.

---

## 1. What is this phase, in one sentence?

We built the assistant's **face**: a sleek, dark-themed desktop app that shows
— in real time — what window the developer is currently working in, reading
the timeline that the Phase 1.1 tracker writes to `data/local_logs.db`.

Phase 1.1 was the *eyes* (silent logging). Phase 1.2 is the *dashboard* the
human actually looks at.

---

## 2. The big picture (how the pieces fit)

```
   Phase 1.1 tracker.py ──writes──▶  data/local_logs.db  (the shared timeline)
                                            │
                                            │ read by a Rust command
                                            ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Tauri (Rust) shell                                        │
   │  src-tauri/src/main.rs                                     │
   │  #[tauri::command] get_active_window()  ── rusqlite ──▶ DB │
   └───────────────────────────┬──────────────────────────────┘
                               │ invoke() bridge (IPC)
                               ▼
   ┌──────────────────────────────────────────────────────────┐
   │  React UI (runs inside Tauri's native webview)             │
   │                                                            │
   │  useActiveWindow()  ─ polls every 2s, non-blocking         │
   │       ├──▶ ActiveWindowCard   (what you're working on)     │
   │       └──▶ StatusIndicator    (ONLINE/OFFLINE + uptime)    │
   └──────────────────────────────────────────────────────────┘
```

When you run it as plain `vite dev` in a browser (no Rust), the **same hook**
silently switches to a **simulation loop** that mocks window changes — so the
UI can be developed and stress-tested without the native backend.

---

## 3. Every tool & library — and why

| Tool / Library | What it is | Why we use it here |
|---|---|---|
| **Tauri v2** | A framework for building desktop apps with a web frontend and a Rust backend. | It uses the OS's **built-in webview** instead of bundling a whole copy of Chromium (like Electron does). Result: app size in **MB not hundreds of MB**, and **RAM in tens of MB not hundreds** — exactly what an always-on developer tool needs. |
| **React 18** | UI library for building components from state. | Declarative components + hooks make the "data changes → UI updates" loop trivial and predictable. |
| **Vite** | Lightning-fast dev server & bundler. | Instant hot-reload in dev, tiny optimized `dist/` in prod. Tauri loads that `dist/` directly. |
| **Tailwind CSS v3** | Utility-first CSS framework. | Lets us build the precise Enterprise Dark Mode palette with design tokens in one config file (`tailwind.config.js`) and zero hand-written CSS drift. |
| **`@tauri-apps/api`** | JS bridge to call Rust. | `invoke("get_active_window")` calls our Rust function and gets JSON back — this is the IPC that connects UI to the database. |
| **`rusqlite` (Rust)** | SQLite client for Rust, `bundled` feature. | The Rust side reads the very same `local_logs.db` the Python tracker writes. `bundled` compiles SQLite in, so there's no system library to install. |

---

## 4. The Enterprise Dark Mode design system

All visual decisions live in **`tailwind.config.js`** as named tokens, so the
look is consistent and tunable from one file:

| Token | Hex | Role |
|---|---|---|
| `nexus-bg` | `#121212` | Deepest structural background |
| `nexus-surface` | `#1e1e1e` | Charcoal card surface |
| `nexus-elevated` | `#262626` | Raised/nested surface, hover |
| `nexus-border` | `#2e2e2e` | Hairline borders |
| `nexus-text` / `muted` / `faint` | `#ececec` / `#9a9a9a` / `#6b6b6b` | Text brightness ramp |
| `nexus-accent` | `#4f9dff` | Bright accent (active app, focus) |
| `nexus-online` / `offline` | `#3ddc84` / `#ff5c5c` | Tracker status |

**Crisp typography** comes from `index.css`: `-webkit-font-smoothing`,
`text-rendering: optimizeLegibility`, and a system-UI font stack (no web-font
download = no flash). **Subtle motion** is limited to a status-dot pulse and a
content fade-in — deliberately no moving/sliding, which keeps the layout calm.

---

## 5. The core functionality, component by component

### a) `useActiveWindow()` — the single data stream (`src/hooks/useActiveWindow.js`)
The brain of the dashboard. Once mounted it polls every **2 seconds** and
exposes `{ entry, status, lastUpdate }`. Two modes, chosen automatically:

- **Native (inside Tauri):** calls `invoke("get_active_window")` → Rust →
  `rusqlite` reads the latest row of `local_logs.db`. On success → `online`;
  on any error (DB missing, tracker off) → `offline`, never a crash.
- **Browser (no Rust):** walks a `MOCK_SEQUENCE` of realistic window titles —
  this is the **Simulation Loop** that proves the layout stays responsive and
  stable as data churns.

Every update is **non-blocking**: we poll on a timer and call `setState` with
already-resolved data. No heavy synchronous work ever runs on the render
thread, so the UI can't stutter.

### b) `ActiveWindowCard` — "what am I working on?" (`src/components/ActiveWindowCard.jsx`)
Shows the app, the window/file title, and when it was seen. Its defining
feature is **zero layout shift with long titles**:

- The card has a **fixed `min-height`** → empty and full states are the same size.
- The title sits in a `min-w-0 flex-1` column using the `truncate-line` utility
  (`overflow-hidden` + `whitespace-nowrap` + `text-ellipsis`). A 300-character
  file path clips to one line with an ellipsis; the card **never grows or
  reflows**. The `min-w-0` is the crucial trick — without it a flex child
  refuses to shrink and the text would push siblings off-screen.
- The full title is preserved in a native `title=` tooltip.

### c) `StatusIndicator` — connectivity + uptime (`src/components/StatusIndicator.jsx`)
A pulsing dot + `ONLINE`/`OFFLINE` label (driven by the hook's `status`), and a
live session clock. The clock uses `useUptime()` and `tabular-nums` so the
digits are fixed-width — the row **never jiggles** as seconds tick.

### d) `useUptime()` — the non-blocking clock (`src/hooks/useUptime.js`)
Stores the session start in a `ref` (survives re-renders), and a single 1-second
interval computes an `MM:SS` string. It only ever sets a tiny string — trivial
work, so it can't block rendering.

### e) `App` — the shell (`src/App.jsx`)
Owns the one data stream and passes slices down to the dumb/reusable cards.
One-directional data flow = exactly one place state changes.

---

## 6. How to run it

```bash
cd frontend
npm install

# --- Option A: pure UI in the browser (uses the simulation loop) ---
npm run dev            # open http://localhost:1420

# --- Option B: the real native desktop app (reads local_logs.db) ---
# one-time: install the Rust toolchain + Linux webview deps
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
sudo apt install libwebkit2gtk-4.1-dev build-essential libssl-dev \
                 libayatana-appindicator3-dev librsvg2-dev
npm run tauri:dev      # launches the native window

# To see live data, run the Phase 1.1 tracker in another terminal:
#   (project root)  ./run_tracker.sh
```

**Test (matches the spec):** start the tracker, launch `npm run tauri:dev`,
then switch between your IDE, a browser, and your terminal — the Active Window
card text updates within ~2 seconds each time, with no layout jump even on long
paths.

---

## 7. File map for Phase 1.2

```
frontend/
├── index.html               # mounts React; <html class="dark">
├── package.json             # scripts + deps (React, Vite, Tailwind, Tauri)
├── vite.config.js           # dev server on port 1420 for Tauri
├── tailwind.config.js       # ← the Enterprise Dark Mode design tokens
├── postcss.config.js        # wires Tailwind + autoprefixer
├── src/
│   ├── main.jsx             # React entrypoint
│   ├── App.jsx              # dashboard shell, owns the data stream
│   ├── index.css            # @tailwind layers + base styles + utilities
│   ├── components/
│   │   ├── ActiveWindowCard.jsx   # long-title-safe activity card
│   │   └── StatusIndicator.jsx    # ONLINE/OFFLINE + uptime clock
│   └── hooks/
│       ├── useActiveWindow.js     # native-or-simulated data stream
│       └── useUptime.js           # non-blocking MM:SS clock
└── src-tauri/               # the native Rust shell
    ├── Cargo.toml           # Rust deps (tauri, rusqlite, serde)
    ├── build.rs
    ├── tauri.conf.json      # window size, dark theme, build hooks
    ├── icons/               # app icons (generate with `npm run tauri icon`)
    └── src/
        └── main.rs          # get_active_window() — reads local_logs.db
```

---

## 8. Why Tauri beats Electron here (the short version)

Electron ships an entire Chromium + Node.js runtime **inside every app** —
~150–250 MB on disk and often 200–500 MB RAM idle, with multiple background
processes. For an assistant meant to run **all day** on a 16 GB i5-6500
alongside Ollama and an IDE, that overhead is unacceptable.

Tauri instead renders in the **OS's existing webview** (WebKitGTK on Linux,
WebView2 on Windows) and ships a small native Rust binary. Typical result:
**a few MB on disk and tens of MB RAM** — leaving headroom for the AI models.
Combined with our **non-blocking** state updates (timer-driven polling, no
heavy work on the render thread), the dashboard stays smooth and invisible in
your resource monitor — exactly the "premium, lightweight" feel the spec
demands.

---

## 9. Known limits & what comes next

- **Polling vs push:** we currently poll the DB every 2s. Phase 2/3 can switch
  to a Tauri **event** (push from Rust → JS) for instant updates and even lower
  overhead.
- **One row shown:** the card shows the latest window. A scrollable "recent
  activity" timeline is a natural next addition.
- **Icons:** run `npm run tauri icon <logo.png>` once to generate real app
  icons before `npm run tauri:build`.
- **Chat & memory:** the AI chat box and ChromaDB recall arrive in Phase 2.
