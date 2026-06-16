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

/// Linux mic fix: WebKitGTK ships with `getUserMedia` disabled and auto-denies
/// every media permission request, so the React mic capture (Phase 6.2) fails
/// with "permission denied". For each window we reach into the underlying
/// WebKitGTK webview to (1) turn on the media-stream setting and (2) grant
/// permission requests. No-op on macOS/Windows, which handle this natively.
#[cfg(target_os = "linux")]
fn enable_webview_media(window: &tauri::WebviewWindow) {
    use webkit2gtk::{PermissionRequestExt, SettingsExt, WebViewExt};

    let _ = window.with_webview(|platform| {
        let webview = platform.inner(); // webkit2gtk::WebView
        if let Some(settings) = WebViewExt::settings(&webview) {
            settings.set_enable_media_stream(true);
            settings.set_enable_mediasource(true);
            settings.set_enable_webaudio(true);
        }
        // Trusted local app → grant mic/camera/etc. requests instead of denying.
        webview.connect_permission_request(|_wv, req| {
            req.allow();
            true
        });
    });
}

/// No-op stub so call sites stay clean on non-Linux platforms.
#[cfg(not(target_os = "linux"))]
fn enable_webview_media(_window: &tauri::WebviewWindow) {}

/// Phase 7.3 initial index: file extensions worth embedding, directories to
/// skip, and a hard cap so a huge workspace can't flood the server.
const INDEX_EXTENSIONS: &[&str] = &[
    "py", "js", "jsx", "ts", "tsx", "rs", "html", "css", "md", "json", "txt", "toml", "yaml",
    "yml", "sh", "go", "java", "c", "cpp", "h", "hpp", "rb", "php", "vue", "svelte",
];
const INDEX_IGNORE_DIRS: &[&str] = &[
    "node_modules", "target", "venv", "__pycache__", "dist", "build", "out", ".cache",
];
const INDEX_MAX_FILES: usize = 300;

/// One-time recursive scan of `root`: read every code/text file and emit it as a
/// `nexus://os-context` event (file fields only) so the client forwards it to
/// the server for embedding. Bounded by [`INDEX_MAX_FILES`]; skips hidden dirs,
/// junk dirs, and non-text/oversized files (via [`read_text_file`]). Runs on its
/// own thread so it never blocks the watcher setup.
fn scan_and_emit(app: &AppHandle, root: &std::path::Path) {
    let mut emitted = 0usize;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = match path.file_name().and_then(|n| n.to_str()) {
                Some(n) => n.to_string(),
                None => continue,
            };
            if path.is_dir() {
                // Skip hidden (.git, .venv, …) and known junk directories.
                if name.starts_with('.') || INDEX_IGNORE_DIRS.contains(&name.as_str()) {
                    continue;
                }
                stack.push(path);
                continue;
            }
            let ext = path
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("")
                .to_lowercase();
            if !INDEX_EXTENSIONS.contains(&ext.as_str()) {
                continue;
            }
            if let Some((file_name, content)) = read_text_file(&path) {
                let payload = OsContextPayload {
                    timestamp: Utc::now().to_rfc3339(),
                    active_window_title: String::new(),
                    active_app_name: String::new(),
                    last_saved_file_name: Some(file_name),
                    file_content: Some(content),
                };
                let _ = app.emit("nexus://os-context", payload);
                emitted += 1;
                if emitted >= INDEX_MAX_FILES {
                    println!("[nexus] workspace scan hit the {emitted}-file cap (stopped early)");
                    return;
                }
            }
        }
    }
    println!("[nexus] workspace scan complete: {emitted} files emitted for indexing");
}

/// Phase 7.3 active-file resolver state: the watched workspace root + a cache of
/// resolved filename→path so we don't re-walk the tree every second.
#[derive(Default)]
struct WorkspaceState {
    root: Option<PathBuf>,
    cache: std::collections::HashMap<String, PathBuf>,
}
type SharedWorkspace = Arc<Mutex<WorkspaceState>>;

/// Pull a likely filename ("models.py") out of a window title like
/// "● models.py — Nexus AI — Visual Studio Code". Mirrors the Python
/// file_resolver: a token must have a dot + a 1-8 char alphabetic-led extension,
/// so "Visual Studio Code" / "Nexus AI" are never mistaken for files.
fn extract_filename(title: &str) -> Option<String> {
    for raw in title.split(|c: char| c.is_whitespace() || c == '—' || c == '|') {
        let tok = raw.trim_matches(|c: char| matches!(c, '●' | '•' | '*' | '◆' | '·'));
        if let Some(dot) = tok.rfind('.') {
            if dot == 0 || dot + 1 >= tok.len() {
                continue;
            }
            let ext = &tok[dot + 1..];
            let alpha_led = ext.chars().next().map(|c| c.is_ascii_alphabetic()).unwrap_or(false);
            if alpha_led && ext.len() <= 8 && ext.chars().all(|c| c.is_ascii_alphanumeric()) {
                return Some(tok.to_string());
            }
        }
    }
    None
}

/// Find a file by basename within the workspace (pruned, budgeted walk).
fn find_file_in_workspace(root: &std::path::Path, name: &str) -> Option<PathBuf> {
    let mut stack = vec![root.to_path_buf()];
    let mut budget = 8000; // bound the walk so a giant tree can't stall the tick
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            budget -= 1;
            if budget <= 0 {
                return None;
            }
            let path = entry.path();
            if path.is_dir() {
                if let Some(n) = path.file_name().and_then(|x| x.to_str()) {
                    if n.starts_with('.') || INDEX_IGNORE_DIRS.contains(&n) {
                        continue;
                    }
                }
                stack.push(path);
            } else if path.file_name().and_then(|x| x.to_str()) == Some(name) {
                return Some(path);
            }
        }
    }
    None
}

/// Resolve the currently focused file from the window title and read its CURRENT
/// content from disk. Returns `(file_name, content)` or None when the focused
/// window isn't a readable file (e.g. a browser). This is what keeps the agent's
/// "currently open file" accurate when the user switches files WITHOUT saving.
fn resolve_active_file(ws: &SharedWorkspace, title: &str) -> Option<(String, String)> {
    let name = extract_filename(title)?;
    let (root, cached) = {
        let s = ws.lock().ok()?;
        (s.root.clone()?, s.cache.get(&name).cloned())
    };
    let path = match cached {
        Some(p) if p.is_file() => p,
        _ => {
            let found = find_file_in_workspace(&root, &name)?;
            if let Ok(mut s) = ws.lock() {
                s.cache.insert(name.clone(), found.clone());
            }
            found
        }
    };
    read_text_file(&path)
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
    workspace: State<'_, SharedWorkspace>,
) -> Result<(), String> {
    let resolved = expand_tilde(&workspace_path);
    let watch_path = PathBuf::from(&resolved);
    if !watch_path.exists() {
        return Err(format!("workspace path does not exist: {resolved}"));
    }

    // Tell the active-file resolver (window thread) which root to search.
    if let Ok(mut ws) = workspace.lock() {
        ws.root = Some(watch_path.clone());
        ws.cache.clear();
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

    // One-time initial index of files already in the workspace, on its own
    // thread so we return immediately (the scan can take a moment).
    let scan_app = app.clone();
    let scan_root = watch_path.clone();
    std::thread::Builder::new()
        .name("nexus-workspace-scan".into())
        .spawn(move || scan_and_emit(&scan_app, &scan_root))
        .ok();

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

    // The orb hosts the same React bundle, so it also needs mic access on Linux.
    if let Some(orb) = app.get_webview_window(ORB_LABEL) {
        enable_webview_media(&orb);
    }

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
        // Phase 6.1/7.3 managed state: shared OS context, watcher handle, and
        // the active-file workspace resolver state.
        .manage::<SharedContext>(Arc::new(Mutex::new(ContextState::default())))
        .manage(WatcherStore(Mutex::new(None)))
        .manage::<SharedWorkspace>(Arc::new(Mutex::new(WorkspaceState::default())))
        .setup(|app| {
            // Linux mic fix: enable media-stream + auto-grant on the main window.
            if let Some(main) = app.get_webview_window("main") {
                enable_webview_media(&main);
            }

            // Spawn the non-blocking active-window poller. It owns clones of the
            // AppHandle and the shared context, ticks every 1000ms forever, and
            // never touches the UI/render thread.
            let handle = app.handle().clone();
            let ctx: SharedContext = app.state::<SharedContext>().inner().clone();
            let ws: SharedWorkspace = app.state::<SharedWorkspace>().inner().clone();

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

                    // Resolve + read the CURRENTLY focused file fresh from disk,
                    // so switching files (even without saving) updates content.
                    let resolved = resolve_active_file(&ws, &title);

                    {
                        let mut s = ctx.lock().expect("os-context state poisoned");
                        s.active_window_title = title;
                        s.active_app_name = app_name;
                        match resolved {
                            Some((fname, content)) => {
                                s.last_saved_file_name = Some(fname);
                                s.file_content = Some(content);
                            }
                            // Not on a readable file (browser, terminal, …) →
                            // clear so a stale file never poses as "open".
                            None => {
                                s.last_saved_file_name = None;
                                s.file_content = None;
                            }
                        }
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
