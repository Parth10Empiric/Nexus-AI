"""
tracker.py — the Nexus AI Background Window Tracker Daemon (Phase 1.1 + 3.1).

The "eyes" of the assistant. It polls the active window every few seconds,
throws away OS noise, and writes a clean timeline of real developer activity
to a local SQLite database (data/local_logs.db).

Phase 3.1 upgrade — the "Active File Source Reader": whenever the focused
window changes, the daemon parses the title (e.g. "views.py - EMPIRA_HR -
VS Code"), maps the project keyword to a workspace root, finds the file on
disk, and stores its raw source text into the `active_file_context` table so
the AI brain (Phase 2) can read exactly what the developer is editing.

Design goals (from the spec):
  * Log active window title + app name every 5 seconds.
  * Only record when the window actually CHANGES (plus periodic heartbeats),
    so the log stays clean.
  * File resolution only runs on a real window switch -> < 1% CPU on i5-6500.

Run it:        python -m tracker.tracker
Stop it:       Ctrl-C  (or send SIGTERM)
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Optional

from . import config, filters
from .db import ActivityStore
from .file_resolver import FileResolver
from .window_source import WindowSample, build_window_source
from .workspace_observer import WorkspaceObserver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus.tracker")


class Tracker:
    def __init__(self) -> None:
        self._source = build_window_source()
        self._store = ActivityStore(config.DB_PATH)
        self._resolver = FileResolver()

        # Phase 5: live-index saved files into the vector vault (optional).
        on_saved = None
        if config.AUTO_INDEX_ON_SAVE:
            try:
                from .vector_indexer import VectorIndexer
                self._indexer = VectorIndexer()
                on_saved = self._indexer.index_single_file
            except Exception as exc:  # missing deps shouldn't break tracking
                log.warning("auto-index disabled (%s)", exc)

        self._observer = WorkspaceObserver(self._store, on_saved=on_saved)
        self._running = True

        # State used for change-detection and heartbeats.
        self._last_key: Optional[tuple[str, str]] = None
        self._last_log_time: float = 0.0

    def _should_log(self, sample: WindowSample, now: float) -> Optional[str]:
        """
        Decide whether to write this sample, and as which event type.
        Returns 'switch', 'heartbeat', or None (skip).
        """
        key = (sample.app_name, sample.title)

        if key != self._last_key:
            return "switch"  # the window changed -> always log

        if (
            config.HEARTBEAT_SECONDS is not None
            and (now - self._last_log_time) >= config.HEARTBEAT_SECONDS
        ):
            return "heartbeat"  # same window, but it's been a while

        return None

    def _tick(self) -> None:
        """One polling iteration."""
        sample = self._source.get_active_window()
        if sample is None:
            return

        if not filters.is_meaningful(sample):
            return

        now = time.monotonic()
        event = self._should_log(sample, now)
        if event is None:
            return

        self._store.log(sample, event=event)
        self._last_key = (sample.app_name, sample.title)
        self._last_log_time = now
        log.info("%-9s %-20s | %s", event, sample.app_name, sample.title)

        # Phase 3.1: only attempt the (more expensive) file resolution on a
        # genuine window switch, never on heartbeats.
        if event == "switch":
            self._capture_file_context(sample)

    def _capture_file_context(self, sample: WindowSample) -> None:
        """
        On a window switch: figure out the active project + file, store an
        initial snapshot (Phase 3.1), and point the watchdog observer at the
        right workspace + active file (Phase 3.2) so subsequent SAVES refresh
        the content event-driven, with no further polling.
        """
        parsed = self._resolver.parse(sample.title)
        if parsed is None or parsed.project_keyword is None:
            return  # not a recognized project file (e.g. a browser tab)

        root = self._resolver.root_for(parsed.project_keyword)

        # Locate + read the file now (initial snapshot). May be None if the
        # file isn't on disk yet or is unchanged since the last read.
        resolved = self._resolver.resolve(sample.title)
        absolute_path = resolved.absolute_path if resolved else None

        if resolved is not None:
            self._store.save_file_context(
                window_title=sample.title,
                app_name=sample.app_name,
                file_name=resolved.file_name,
                absolute_path=resolved.absolute_path,
                file_content=resolved.content,
            )
            log.info("file ctx  %-20s | %s (%d chars)",
                     resolved.file_name, resolved.absolute_path,
                     len(resolved.content))

        # Hand the active context to the observer. If the project root changed
        # it will gracefully swap which directory it watches.
        self._observer.set_active_context(
            root=root,
            file_name=parsed.file_name,
            absolute_path=absolute_path,
            window_title=sample.title,
            app_name=sample.app_name,
        )

    def run(self) -> None:
        self._install_signal_handlers()
        log.info("Nexus tracker started. Writing to %s", config.DB_PATH)
        log.info("Polling every %ss. Press Ctrl-C to stop.",
                 config.POLL_INTERVAL_SECONDS)
        self._observer.start()
        try:
            while self._running:
                try:
                    self._tick()
                except Exception as exc:  # never let one bad poll kill the daemon
                    log.warning("tick failed: %s", exc)
                time.sleep(config.POLL_INTERVAL_SECONDS)
        finally:
            self._observer.stop()
            self._store.close()
            log.info("Nexus tracker stopped cleanly.")

    def _install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            log.info("Received signal %s, shutting down...", signum)
            self._running = False

        try:
            signal.signal(signal.SIGINT, _stop)
            signal.signal(signal.SIGTERM, _stop)
        except ValueError:
            # signal.signal only works in the main thread — when the tracker is
            # launched as a background thread (run_nexus.py) we skip this and let
            # the parent process own shutdown.
            log.debug("signal handlers skipped (not main thread)")


def main() -> int:
    try:
        Tracker().run()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
