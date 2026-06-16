# Phase 6.1 — Native "Client Eyes" (Rust core inside Tauri)

**Goal:** make window-tracking + file-watching run *natively in the Tauri background
process* so the distributed `.deb` is self-contained. This removes the Python
`tracker` daemon from the critical path — the app no longer needs
`python run_nexus.py` running for the "eyes" to work on a remote Ubuntu box.

This is the first step of the Phase 6 transition to a Client-Server SaaS model:
the **client** now sees the OS by itself and emits structured context to the UI.

## What was built

| Piece | File | Notes |
|-------|------|-------|
| Crate deps | `frontend/src-tauri/Cargo.toml` | `active-win-pos-rs = "0.8"`, `notify = "6.1"`, `chrono = "0.4"` |
| Native eyes | `frontend/src-tauri/src/main.rs` | 1000ms window poller + `start_file_watcher` command |
| React bridge | `frontend/src/hooks/useOsContext.js` | listens to `nexus://os-context`, console-logs payload |

## Architecture

```
┌────────────────────────── Tauri (Rust) process ──────────────────────────┐
│                                                                           │
│  setup() ── spawns "nexus-window-eyes" thread ──┐                         │
│                  every 1000ms:                  │                         │
│                  active_win_pos_rs::get_active_window()                    │
│                                                 ▼                         │
│  start_file_watcher(path)  ──notify──▶   Arc<Mutex<ContextState>>         │
│   (recursive inotify)        on Modify         │  (shared snapshot)       │
│        read_text_file()  ───────────────────────┘                         │
│         (≤1MB, UTF-8 only)                      │                         │
│                                                 ▼                         │
│                                   emit("nexus://os-context", payload)     │
└─────────────────────────────────────────────────│─────────────────────────┘
                                                   ▼
                              React  useOsContext()  →  console.log(payload)
```

Both triggers (1000ms tick **or** file save) merge into one `ContextState` and
emit the **same** unified payload, so the frontend has a single source of truth.

## Event contract — `nexus://os-context`

```json
{
  "timestamp": "2026-06-16T10:22:31.482+00:00",
  "active_window_title": "main.rs — Nexus AI — VS Code",
  "active_app_name": "code",
  "last_saved_file_name": "main.rs",
  "file_content": "use std::..."
}
```

`last_saved_file_name` / `file_content` are `null` until the first save is seen
by a watcher started via `start_file_watcher`.

## Safety guards

- **Window capture** degrades to empty strings on failure / unsupported sessions
  (e.g. some Wayland setups) — never panics, never stops the loop.
- **File reads** are skipped unless the file is a regular file, non-empty,
  ≤ 1 MiB, and valid UTF-8 — this is the binary-file + huge-file crash guard.
- **Watcher lifetime**: the `RecommendedWatcher` is stored in managed
  `WatcherStore` state; `notify` stops the instant it is dropped, so we must keep
  it alive. Re-calling `start_file_watcher` cleanly replaces the old watcher.
- **Non-blocking**: the poller runs on a dedicated OS thread; the watcher
  callback runs on notify's own thread. The UI/render thread is never touched.

## Frontend usage

```jsx
import { useOsContext } from "./hooks/useOsContext.js";

function Example() {
  const { context, startFileWatcher } = useOsContext();
  useEffect(() => { startFileWatcher("~/Projects/my-app"); }, []);
  return <pre>{JSON.stringify(context, null, 2)}</pre>;
}
```

## How to test

### 0. Prerequisites (Linux/X11)
The active-window crate uses X11. On Wayland, `get_active_window()` returns an
error and the title/app degrade to empty strings (by design) — log into an
"Ubuntu on Xorg" session for the title to populate. No `apt` package is needed
for the file watcher (`notify` uses inotify directly).

### 1. Type-check the Rust core (fast, no GUI)
```bash
cd frontend/src-tauri && cargo check
```
Expect `Finished` with no errors.

### 2. Wire a temporary probe into the UI
Add this to any mounted component (e.g. the top of `src/App.jsx`) just to watch
the event stream — remove it after testing:
```jsx
import { useOsContext } from "./hooks/useOsContext.js";
// inside the component:
const { context, startFileWatcher } = useOsContext();
useEffect(() => { startFileWatcher("~/Projects/Nexus AI"); }, [startFileWatcher]);
```

### 3. Run the native app
```bash
cd frontend && npm run tauri:dev
```

### 4. Test the active-window poller (every 1000ms)
- Open DevTools (right-click → Inspect) → **Console**.
- A `[nexus://os-context]` log appears **once per second**.
- Click between windows (VS Code, a browser, a terminal) and confirm
  `active_window_title` / `active_app_name` change to match the focused window
  on the next tick.

### 5. Test the file watcher (on save)
- With `startFileWatcher("~/Projects/Nexus AI")` active from step 2, open a text
  file in that tree and **save it**.
- The next event should now carry `last_saved_file_name` (e.g. `"main.rs"`) and
  the file's text in `file_content`.

### 6. Test the safety guards
- **Binary skip:** save/copy a `.png` or `.zip` into the watched tree → no
  `file_content` update (UTF-8 guard rejects it).
- **Size cap:** `truncate -s 2M big.txt` inside the tree → skipped (> 1 MiB).
- **Bad path:** call `startFileWatcher("/does/not/exist")` → the promise rejects
  with `workspace path does not exist: ...` (catch it in the UI).
- **Re-watch leak-free:** call `startFileWatcher` on a second path; saves in the
  first path stop firing, saves in the new path fire (old watcher was dropped).

### 7. Production sanity (optional)
`npm run tauri:build`, install the `.deb`, launch from the app menu (no terminal,
no Python). The window/file events still fire — proving the "eyes" are now fully
native and self-contained.

## Not yet migrated (still Python, future Phase 6.x)

The "brain/voice/memory" half is still in `run_nexus.py` (Ollama orchestration,
STT/TTS, Chroma vector memory over `ws://127.0.0.1:8765`). Phase 6.1 only moves
the **eyes**. The voice/chat agent still shows "backend off" until that half is
either bundled as a sidecar or moved server-side in a later Phase 6 step.
