"""
workspace_observer.py — Phase 3.2 "File-Change Event Hook".

Instead of re-reading the active file on every poll, we let the operating
system tell us when a file is saved. `watchdog` runs an OS-level filesystem
monitor (inotify on Linux) on a background thread and calls us back on
modification. We then read the file ONCE, after a short debounce, and refresh
its snapshot in the `active_file_context` table.

Two threads cooperate:
  * MAIN thread  — the window tracker. On each window switch it calls
    `set_active_context(...)`, telling the observer which file is active and
    which workspace root to watch. If the project changed, the observer
    gracefully swaps its watched directory.
  * OBSERVER thread — owned by watchdog. It receives raw FS events, debounces
    them, and (only for the active file) reads + stores the new content.

All shared state is protected by a single re-entrant-free Lock, and the
database writes are serialized inside ActivityStore's own lock, so the design
is fully thread-safe.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config
from .db import ActivityStore
from .file_resolver import is_path_ignored, read_text_with_guardrails

log = logging.getLogger("nexus.tracker.observer")


class _DebouncedSaveHandler(FileSystemEventHandler):
    """
    Translates noisy watchdog modification events into clean, debounced
    "this file finished saving" callbacks.

    Editors fire several events per save (write buffer, swap file, atomic
    rename, metadata touch). We use a TRAILING-EDGE debounce: each event for a
    path (re)starts a per-path timer; the callback fires only after the path
    has been quiet for `debounce_seconds`. That collapses an entire save burst
    into exactly one read, and guarantees we read AFTER the write settles.
    """

    def __init__(self, debounce_seconds: float, on_saved) -> None:
        super().__init__()
        self._debounce = debounce_seconds
        self._on_saved = on_saved
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, src_path: str) -> None:
        path = Path(src_path)
        if is_path_ignored(path):
            return  # cheap reject before we ever touch disk

        with self._lock:
            existing = self._timers.get(src_path)
            if existing is not None:
                existing.cancel()  # reset the cooldown — burst still in progress
            timer = threading.Timer(self._debounce, self._fire, args=(src_path,))
            timer.daemon = True
            self._timers[src_path] = timer
            timer.start()

    def _fire(self, src_path: str) -> None:
        with self._lock:
            self._timers.pop(src_path, None)
        try:
            self._on_saved(Path(src_path))
        except Exception as exc:  # a bad callback must never kill the thread
            log.warning("save handler failed for %s: %s", src_path, exc)

    # watchdog callbacks ----------------------------------------------------
    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._schedule(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        # Some editors save via atomic write+rename, surfacing as 'created'.
        if event.is_directory:
            return
        self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic-save rename: the destination is the real saved file.
        dest = getattr(event, "dest_path", None)
        if dest and not event.is_directory:
            self._schedule(dest)

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class WorkspaceObserver:
    """
    Owns a single watchdog Observer thread and points it at whichever workspace
    root is currently active. Reads + stores the active file's content whenever
    it is saved.
    """

    def __init__(self, store: ActivityStore,
                 debounce_seconds: float = config.OBSERVER_DEBOUNCE_SECONDS,
                 on_saved=None) -> None:
        self._store = store
        # Optional hook called with the saved file path AFTER its content is
        # stored — used to live-index the file into the vector vault (Phase 5).
        self._on_saved = on_saved
        self._observer = Observer()
        self._handler = _DebouncedSaveHandler(debounce_seconds, self._on_file_saved)

        # Guarded shared state (touched by main + observer threads).
        self._state_lock = threading.Lock()
        self._watch = None                       # current watchdog ObservedWatch
        self._current_root: Optional[Path] = None
        self._active_path: Optional[Path] = None
        self._active_meta: dict = {}
        self._started = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self._started:
            self._observer.start()
            self._started = True
            log.info("watchdog observer thread started.")

    def stop(self) -> None:
        self._handler.cancel_all()
        if self._started:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._started = False
            log.info("watchdog observer thread stopped.")

    # -- called by the MAIN (window-tracker) thread -------------------------
    def set_active_context(
        self,
        *,
        root: Optional[Path],
        file_name: Optional[str],
        absolute_path: Optional[str],
        window_title: str,
        app_name: str,
    ) -> None:
        """
        Update which file/project is active. If the workspace root changed,
        gracefully swap the watched directory. Safe to call on every switch.
        """
        with self._state_lock:
            self._active_path = Path(absolute_path) if absolute_path else None
            self._active_meta = {
                "file_name": file_name or (
                    self._active_path.name if self._active_path else ""
                ),
                "window_title": window_title,
                "app_name": app_name,
            }
            if root != self._current_root:
                self._swap_watch_locked(root)

    def _swap_watch_locked(self, new_root: Optional[Path]) -> None:
        """Unschedule the old directory and schedule the new one. Caller holds
        the state lock."""
        # Tear down the previous watch (if any).
        if self._watch is not None:
            try:
                self._observer.unschedule(self._watch)
            except Exception as exc:
                log.warning("unschedule failed: %s", exc)
            finally:
                self._watch = None
            self._handler.cancel_all()  # drop stale pending timers for old tree

        # Stand up the new watch (if the new root is valid).
        if new_root is not None and new_root.is_dir():
            try:
                self._watch = self._observer.schedule(
                    self._handler, str(new_root), recursive=True
                )
                log.info("now watching workspace: %s", new_root)
            except Exception as exc:
                log.warning("schedule failed for %s: %s", new_root, exc)
                self._watch = None

        self._current_root = new_root

    # -- called by the OBSERVER thread (via the debounced handler) ----------
    def _on_file_saved(self, path: Path) -> None:
        """A file in the watched tree finished saving. If it's the active file,
        read and store its fresh content."""
        with self._state_lock:
            active = self._active_path
            meta = dict(self._active_meta)

        if active is None:
            return
        # Compare resolved paths so symlinks/././ variants still match.
        try:
            if path.resolve() != active.resolve():
                return  # a save in the tree, but not the file we care about
        except OSError:
            return

        content = read_text_with_guardrails(path)
        if content is None:
            return  # too big, binary, or unreadable -> skip silently

        self._store.save_file_context(
            window_title=meta.get("window_title", ""),
            app_name=meta.get("app_name", ""),
            file_name=meta.get("file_name", path.name),
            absolute_path=str(path),
            file_content=content,
        )
        log.info("save -> ctx  %-18s | %s (%d chars)",
                 meta.get("file_name", path.name), path, len(content))

        # Live-index the saved file into the vector vault (deduped). Never let
        # an indexing error disturb the core save/track loop.
        if self._on_saved is not None:
            try:
                self._on_saved(str(path))
            except Exception as exc:
                log.warning("on_saved (index) hook failed: %s", exc)
