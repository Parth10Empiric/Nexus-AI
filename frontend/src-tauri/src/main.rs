// Prevent a console window from popping up on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

/// Window label for the floating agent orb. Used to spawn, look up, and
/// destroy the second window so we never accidentally create two of them.
const ORB_LABEL: &str = "agent-orb";

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
        .invoke_handler(tauri::generate_handler![
            get_active_window,
            get_active_file_context,
            spawn_agent_orb,
            close_agent_orb
        ])
        .run(tauri::generate_context!())
        .expect("error while running Nexus AI dashboard");
}
