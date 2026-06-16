// Prevent a console window from popping up on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use active_win_pos_rs::get_active_window as get_os_active_window;
use chrono::Utc;
use notify::{recommended_watcher, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};

/// Window label for the floating agent orb. Used to spawn, look up, and
/// destroy the second window so we never accidentally create two of them.
const ORB_LABEL: &str = "agent-orb";

/// Hard ceiling on how much of a saved file we read into memory. Anything
/// larger (or non-UTF-8 / binary) is skipped so a multi-gigabyte save can never
/// blow up the background watcher thread. 1 MiB comfortably covers source code.
const MAX_FILE_BYTES: u64 = 1024 * 1024;

/// Tick interval for the active-window poller (Phase 6.1 spec: exactly 1000ms).
const WINDOW_POLL_MS: u64 = 1000;

/// One row of the activity timeline, shaped exactly how the React hook expects.
#[derive(Serialize)]
struct WindowEntry {
    ts_utc: String,
    app_name: String,
    title: String,
    event: String,
}

/// Phase 3.3: the live code context handed to the prompt mixer. `None` is
/// returned when there is no context OR when the self-exclusion guard fires.
#[derive(Serialize)]
struct FileContext {
    file_name: String,
    absolute_path: String,
    file_content: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// Phase 6.1 — Native "Client Eyes": background window tracker + file watcher
//
// Replaces the Python tracker daemon entirely so the distributed .deb is fully
// self-contained. A 1000ms background thread captures the active window; a
// React-triggered `notify` watcher captures file saves. Both funnel into one
// shared state and emit a single unified `nexus://os-context` Tauri event.
// ─────────────────────────────────────────────────────────────────────────────

/// The unified payload emitted to the React frontend on every window tick OR
/// file save. Shape is contractually fixed (see Phase 6.1 spec) — do not
/// reorder/rename fields without also updating `useOsContext.js`.
#[derive(Serialize, Clone)]
struct OsContextPayload {
    /// RFC-3339 / ISO-8601 timestamp of when this snapshot was emitted.
    timestamp: String,
    active_window_title: String,
    active_app_name: String,
    /// `null` until the first file save is observed by the watcher.
    last_saved_file_name: Option<String>,
    /// `null` until the first (text/code) file save is read successfully.
    file_content: Option<String>,
}

/// Mutable, thread-shared snapshot of "what the OS is doing right now". The
/// window thread writes the window fields; the file watcher writes the file
/// fields; either one emits the combined view. A plain Mutex is ample — writes
/// are sub-millisecond and at most once per second + once per save.
#[derive(Default)]
struct ContextState {
    active_window_title: String,
    active_app_name: String,
    last_saved_file_name: Option<String>,
    file_content: Option<String>,
}

/// Tauri-managed handle to the shared OS context. Cloned into the window thread
/// and into the file-watcher closure so all three see the same state.
type SharedContext = Arc<Mutex<ContextState>>;

/// Keeps the active `RecommendedWatcher` alive for the lifetime of the app.
/// `notify` stops delivering events the instant the watcher is dropped, so we
/// must stash it in managed state rather than letting it fall out of the
/// command's stack frame.
struct WatcherStore(Mutex<Option<RecommendedWatcher>>);

/// Build a fresh payload from the current shared state and emit it to ALL
/// windows over the global event bus. Cloning under the lock keeps the critical
/// section tiny; the actual `emit` happens after the guard is dropped.
fn emit_os_context(app: &AppHandle, ctx: &SharedContext) {
    let payload = {
        let s = ctx.lock().expect("os-context state poisoned");
        OsContextPayload {
            timestamp: Utc::now().to_rfc3339(),
            active_window_title: s.active_window_title.clone(),
            active_app_name: s.active_app_name.clone(),
            last_saved_file_name: s.last_saved_file_name.clone(),
            file_content: s.file_content.clone(),
        }
    };
    // Non-blocking, fire-and-forget. A failed emit (e.g. during shutdown) must
    // never crash the background thread, so we deliberately ignore the result.
    let _ = app.emit("nexus://os-context", payload);
}

/// Expand a leading `~` to the user's home directory so the React side can pass
/// friendly paths like `~/Projects/my-app`. Everything else is returned as-is.
fn expand_tilde(path: &str) -> String {
    if let Some(rest) = path.strip_prefix('~') {
        if let Ok(home) = std::env::var("HOME") {
            return format!("{home}{rest}");
        }
    }
    path.to_string()
}

/// Safely read a saved file as UTF-8 text, applying every guard the spec asks
/// for. Returns `(file_name, contents)` or `None` when the file should be
/// skipped: missing, empty, larger than [`MAX_FILE_BYTES`], or binary (i.e. not
/// valid UTF-8). This is what keeps the watcher from ever crashing on a binary
/// blob or a multi-gigabyte artifact.
fn read_text_file(path: &Path) -> Option<(String, String)> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() == 0 || meta.len() > MAX_FILE_BYTES {
        return None;
    }
    // `from_utf8` is the binary-file guard: PNG/zip/etc. fail here -> None.
    let content = String::from_utf8(std::fs::read(path).ok()?).ok()?;
    let name = path.file_name()?.to_string_lossy().into_owned();
    Some((name, content))
}

/// The infinite-loop guardrail (mirror of context_mixer.is_self_referential).
/// True if the active file belongs to Nexus AI's OWN codebase.
fn is_self_referential(absolute_path: &str, window_title: &str) -> bool {
    let hay = format!("{} {}", absolute_path, window_title).to_lowercase();
    ["nexus ai", "nexus_ai", "nexus-ai"]
        .iter()
        .any(|m| hay.contains(m))
}

/// Locate `data/local_logs.db`. The dashboard lives in `<root>/frontend`, and
/// the tracker writes to `<root>/data/local_logs.db`. In dev the working dir is
/// `frontend/src-tauri`, so we walk up looking for the `data` folder. A
/// `NEXUS_DB_PATH` env var overrides everything for custom deployments.
fn resolve_db_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("NEXUS_DB_PATH") {
        return Some(PathBuf::from(p));
    }

    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..6 {
        let candidate = dir.join("data").join("local_logs.db");
        if candidate.exists() {
            return Some(candidate);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

/// Tauri command exposed to the frontend. Returns the most recent meaningful
/// window the Phase 1.1 tracker logged, or `None` if there is no data yet.
#[tauri::command]
fn get_active_window() -> Result<Option<WindowEntry>, String> {
    let db_path = match resolve_db_path() {
        Some(p) => p,
        None => return Err("local_logs.db not found — is the tracker running?".into()),
    };

    let conn = rusqlite::Connection::open(&db_path).map_err(|e| e.to_string())?;

    let mut stmt = conn
        .prepare(
            "SELECT ts_utc, app_name, title, event \
             FROM activity_log ORDER BY id DESC LIMIT 1",
        )
        .map_err(|e| e.to_string())?;

    let mut rows = stmt.query([]).map_err(|e| e.to_string())?;

    if let Some(row) = rows.next().map_err(|e| e.to_string())? {
        Ok(Some(WindowEntry {
            ts_utc: row.get(0).map_err(|e| e.to_string())?,
            app_name: row.get(1).map_err(|e| e.to_string())?,
            title: row.get(2).map_err(|e| e.to_string())?,
            event: row.get(3).map_err(|e| e.to_string())?,
        }))
    } else {
        Ok(None)
    }
}

/// Phase 3.3 command: fetch the latest active-file context for prompt mixing,
/// applying the Nexus AI self-exclusion guard. Returns Ok(None) when the file
/// is self-referential or there is nothing to inject. Single indexed row read
/// -> sub-millisecond, so the frontend mixer stays well under its 5ms budget.
#[tauri::command]
fn get_active_file_context() -> Result<Option<FileContext>, String> {
    let db_path = match resolve_db_path() {
        Some(p) => p,
        None => return Ok(None),
    };

    let conn = rusqlite::Connection::open(&db_path).map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare(
            "SELECT window_title, file_name, absolute_path, file_content \
             FROM active_file_context ORDER BY ts_utc DESC LIMIT 1",
        )
        .map_err(|e| e.to_string())?;
    let mut rows = stmt.query([]).map_err(|e| e.to_string())?;

    if let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let window_title: String = row.get(0).map_err(|e| e.to_string())?;
        let file_name: String = row.get(1).map_err(|e| e.to_string())?;
        let absolute_path: String = row.get(2).map_err(|e| e.to_string())?;
        let file_content: String = row.get(3).map_err(|e| e.to_string())?;

        // CRITICAL: never inject our own source -> prevents recursive loops.
        if is_self_referential(&absolute_path, &window_title) {
            return Ok(None);
        }

        Ok(Some(FileContext {
            file_name,
            absolute_path,
            file_content,
        }))
    } else {
        Ok(None)
    }
}

/// Phase 6.1 command: start a recursive native filesystem watcher on
/// `workspace_path` (which may begin with `~`). On every file-modify event the
/// saved file is read (with the binary/size guards in [`read_text_file`]),
/// merged into the shared OS context, and a fresh `nexus://os-context` event is
/// emitted to React.
///
/// Calling this again replaces the previous watcher — the old
/// `RecommendedWatcher` is dropped (and thus stops) the moment the new one is
/// stored, so switching workspaces is safe and leak-free.
#[tauri::command]
fn start_file_watcher(
    workspace_path: String,
    app: AppHandle,
    ctx: State<'_, SharedContext>,
    store: State<'_, WatcherStore>,
) -> Result<(), String> {
    let resolved = expand_tilde(&workspace_path);
    let watch_path = PathBuf::from(&resolved);
    if !watch_path.exists() {
        return Err(format!("workspace path does not exist: {resolved}"));
    }

    // Clones captured by the notify callback (runs on notify's own thread).
    let app_handle = app.clone();
    let ctx_arc: SharedContext = ctx.inner().clone();

    let mut watcher = recommended_watcher(move |res: notify::Result<notify::Event>| {
        let event = match res {
            Ok(e) => e,
            Err(_) => return, // transient inotify hiccup — drop it, never panic
        };

        // We only care about content writes (saves), not metadata/access.
        if !matches!(event.kind, EventKind::Modify(_)) {
            return;
        }

        // A single save can surface multiple paths; emit for the first one that
        // is a readable text/code file.
        for path in event.paths {
            if let Some((name, content)) = read_text_file(&path) {
                {
                    let mut s = ctx_arc.lock().expect("os-context state poisoned");
                    s.last_saved_file_name = Some(name);
                    s.file_content = Some(content);
                }
                emit_os_context(&app_handle, &ctx_arc);
                break;
            }
        }
    })
    .map_err(|e| e.to_string())?;

    watcher
        .watch(&watch_path, RecursiveMode::Recursive)
        .map_err(|e| e.to_string())?;

    // Keep it alive: dropping a RecommendedWatcher silently stops all events.
    *store.0.lock().map_err(|e| e.to_string())? = Some(watcher);
    Ok(())
}

/// UI Phase 1 — spawn the Floating Agent Widget.
///
/// Creates a second, borderless, transparent, always-on-top window that hosts
/// the React `<AgentOrb />` (the same Vite bundle, routed via the
/// `?window=orb` query string). If the orb already exists we just re-show and
/// focus it — clicking "Start Agent" twice must never stack two orbs.
///
/// `orb://opened` is emitted to ALL windows so the dashboard toggle can reflect
/// the live lifecycle (mirrors `close_agent_orb` below).
#[tauri::command]
fn spawn_agent_orb(app: AppHandle) -> Result<(), String> {
    if let Some(existing) = app.get_webview_window(ORB_LABEL) {
        let _ = existing.show();
        let _ = existing.set_focus();
        let _ = app.emit("orb://opened", ());
        return Ok(());
    }

    WebviewWindowBuilder::new(
        &app,
        ORB_LABEL,
        WebviewUrl::App("index.html?window=orb".into()),
    )
    .title("Nexus Agent")
    .inner_size(168.0, 168.0)
    .decorations(false) // no title bar / chrome
    .transparent(true) // desktop shows through the empty corners
    .always_on_top(true) // never hides behind VS Code / the browser
    .skip_taskbar(true) // it's an overlay, not an app in the switcher
    .resizable(false)
    .shadow(false) // we draw our own glow in CSS
    .focused(true)
    .position(1200.0, 120.0) // top-right by default; user drags from here
    .build()
    .map_err(|e| e.to_string())?;

    let _ = app.emit("orb://opened", ());
    Ok(())
}

/// UI Phase 1 — destroy the Floating Agent Widget ("Close Agent" menu item, or
/// the dashboard toggle turning off). Closing is idempotent: if the orb is
/// already gone we still emit `orb://closed` so the dashboard converges to the
/// off state.
#[tauri::command]
fn close_agent_orb(app: AppHandle) -> Result<(), String> {
    if let Some(orb) = app.get_webview_window(ORB_LABEL) {
        orb.close().map_err(|e| e.to_string())?;
    }
    let _ = app.emit("orb://closed", ());
    Ok(())
}

fn main() {
    tauri::Builder::default()
        // Phase 6.1 managed state: shared OS context + the live watcher handle.
        .manage::<SharedContext>(Arc::new(Mutex::new(ContextState::default())))
        .manage(WatcherStore(Mutex::new(None)))
        .setup(|app| {
            // Spawn the non-blocking active-window poller. It owns clones of the
            // AppHandle and the shared context, ticks every 1000ms forever, and
            // never touches the UI/render thread.
            let handle = app.handle().clone();
            let ctx: SharedContext = app.state::<SharedContext>().inner().clone();

            std::thread::Builder::new()
                .name("nexus-window-eyes".into())
                .spawn(move || loop {
                    // Capture the active window; degrade gracefully to empty
                    // strings on failure or unsupported environments (e.g. some
                    // Wayland sessions) rather than panicking.
                    let (title, app_name) = match get_os_active_window() {
                        Ok(w) => (w.title, w.app_name),
                        Err(_) => (String::new(), String::new()),
                    };

                    {
                        let mut s = ctx.lock().expect("os-context state poisoned");
                        s.active_window_title = title;
                        s.active_app_name = app_name;
                    }

                    emit_os_context(&handle, &ctx);
                    std::thread::sleep(Duration::from_millis(WINDOW_POLL_MS));
                })
                .expect("failed to spawn window-eyes thread");

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_active_window,
            get_active_file_context,
            start_file_watcher,
            spawn_agent_orb,
            close_agent_orb
        ])
        .run(tauri::generate_context!())
        .expect("error while running Nexus AI dashboard");
}
