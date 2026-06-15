"""
file_resolver.py — Phase 3.1 "Active File Source Reader".

Turns a raw window title like

    "● views.py - EMPIRA_HR - Visual Studio Code"

into the absolute path of the file on disk and its source text:

    /home/empiric/Projects/EMPIRA_HR/backend/views.py  +  <file contents>

It does this in four steps:
    1. parse_title()      -> extract the file name + project keyword via regex
    2. workspace lookup   -> map the project keyword to an absolute root dir
    3. _search_workspace()-> a pruned os.walk that skips node_modules/.git/etc.
    4. _read_text_file()  -> guarded read (size limit, binary detection)

Everything here uses only the Python standard library, so there is no extra
dependency and no measurable overhead beyond a directory walk that only runs
when the active window actually changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("nexus.tracker.resolver")

# ---------------------------------------------------------------------------
# Regex architecture
# ---------------------------------------------------------------------------
# A "modified" marker some editors prepend to an unsaved file's title
# (bullet, asterisk, filled circle, diamond) plus leading whitespace.
_DIRTY_MARKER = re.compile(r"^[\s•\*●◆●]+")

# A filename = one or more name chars, a dot, then a 1-8 char extension.
# We deliberately require an extension so plain words ("Visual Studio Code")
# are never mistaken for files. Captured group 1 is the filename.
_FILENAME_RE = re.compile(r"([\w.\-+#]+\.[A-Za-z0-9]{1,8})(?:$|\s)")

# Editors usually separate title segments with " - " (also "—" / " | ").
_SEGMENT_SPLIT = re.compile(r"\s+[-—|]\s+")


@dataclass(frozen=True)
class ParsedTitle:
    file_name: str
    project_keyword: Optional[str]


# ---------------------------------------------------------------------------
# Shared guardrail helpers (used by both the Phase 3.1 resolver and the
# Phase 3.2 watchdog observer, so the rules live in exactly one place).
# ---------------------------------------------------------------------------
def is_path_ignored(path: Path) -> bool:
    """True if any part of the path is an ignored dir/dotfolder, or the file
    has an ignored (binary/lock) extension."""
    if path.suffix.lower() in config.IGNORED_FILE_EXTENSIONS:
        return True
    for part in path.parts:
        if part in config.IGNORED_DIRS:
            return True
        # Skip dot-directories (.git, .venv, .cache, …) but not dotfiles
        # like ".eslintrc.json" which are legitimate source.
        if part.startswith(".") and part not in (".", "..") and "." not in part[1:]:
            if part != path.name:  # a dot *directory* in the path
                return True
    return False


def read_text_with_guardrails(path: Path) -> Optional[str]:
    """
    Read a file's text with every safety constraint applied, WITHOUT any
    caching. Returns the decoded text, or None if the file is ignored, too
    large (>500KB), binary, or unreadable.

    This is the single, authoritative "is this file safe to read?" function.
    """
    if is_path_ignored(path):
        return None

    try:
        stat = path.stat()
    except OSError as exc:
        log.warning("stat failed for %s: %s", path, exc)
        return None

    if not path.is_file():
        return None

    if stat.st_size > config.MAX_FILE_SIZE_BYTES:
        log.info("skipping %s (%d bytes > limit)", path, stat.st_size)
        return None

    try:
        data = path.read_bytes()  # bytes first, so we can sniff for binary
    except OSError as exc:
        log.warning("read failed for %s: %s", path, exc)
        return None

    if b"\x00" in data[:8192]:
        log.info("skipping %s (looks binary)", path)
        return None

    # Decode leniently: a stray bad byte must never crash the daemon.
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ResolvedFile:
    file_name: str
    absolute_path: str
    content: str


def parse_title(title: str, workspace_keywords: list[str]) -> Optional[ParsedTitle]:
    """
    Extract (file_name, project_keyword) from a window title.

    Returns None if no plausible file name is present (e.g. a browser tab).
    project_keyword may be None if the title carries a file but no known
    project keyword — the caller can then decide whether to skip it.
    """
    if not title:
        return None

    clean = _DIRTY_MARKER.sub("", title).strip()

    # The file name is almost always in the FIRST segment for editors
    # (VS Code: "<file> - <project> - <app>"). Search the whole title as a
    # fallback, but prefer the leftmost match.
    segments = _SEGMENT_SPLIT.split(clean)
    file_name: Optional[str] = None
    for seg in segments:
        m = _FILENAME_RE.search(seg.strip())
        if m:
            file_name = m.group(1)
            break
    if file_name is None:
        m = _FILENAME_RE.search(clean)
        file_name = m.group(1) if m else None
    if file_name is None:
        return None

    # Project keyword: the first configured keyword that appears in the title
    # (case-insensitive). This is robust to ordering and editor differences.
    lowered = clean.lower()
    project_keyword = None
    for kw in workspace_keywords:
        if kw.lower() in lowered:
            project_keyword = kw
            break

    return ParsedTitle(file_name=file_name, project_keyword=project_keyword)


class FileResolver:
    """Resolves window titles to on-disk files and reads them safely."""

    def __init__(self) -> None:
        self._workspace_map: dict[str, Path] = self._load_workspace_map()
        # Cache of the last resolved (path, mtime) so we don't re-read an
        # unchanged file every time the user returns to the same window.
        self._cache_path: Optional[str] = None
        self._cache_mtime: Optional[float] = None

    # -- configuration ------------------------------------------------------
    @staticmethod
    def _load_workspace_map() -> dict[str, Path]:
        raw: dict[str, str]
        path = config.WORKSPACE_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data.get("workspaces", {})
            if not isinstance(raw, dict) or not raw:
                raise ValueError("no 'workspaces' object")
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "workspace_config.json unusable (%s); using built-in defaults.",
                exc,
            )
            raw = config.DEFAULT_WORKSPACE_MAP

        resolved: dict[str, Path] = {}
        for keyword, root in raw.items():
            p = Path(root).expanduser()
            if p.is_dir():
                resolved[keyword] = p
            else:
                log.warning("workspace '%s' -> %s does not exist; skipping.",
                            keyword, p)
        return resolved

    @property
    def keywords(self) -> list[str]:
        return list(self._workspace_map.keys())

    def parse(self, title: str) -> Optional[ParsedTitle]:
        """Public wrapper around parse_title using the loaded keywords."""
        return parse_title(title, self.keywords)

    def root_for(self, project_keyword: Optional[str]) -> Optional[Path]:
        """Absolute workspace root for a project keyword, or None."""
        if not project_keyword:
            return None
        return self._workspace_map.get(project_keyword)

    # -- public API ---------------------------------------------------------
    def resolve(self, title: str) -> Optional[ResolvedFile]:
        """
        Full pipeline: title -> ResolvedFile, or None if nothing usable.
        Safe to call on every window change; never raises.
        """
        try:
            parsed = parse_title(title, self.keywords)
            if parsed is None or parsed.project_keyword is None:
                return None

            root = self._workspace_map.get(parsed.project_keyword)
            if root is None:
                return None

            abs_path = self._search_workspace(root, parsed.file_name)
            if abs_path is None:
                return None

            content = self._read_text_file(abs_path)
            if content is None:
                return None

            return ResolvedFile(
                file_name=parsed.file_name,
                absolute_path=str(abs_path),
                content=content,
            )
        except Exception as exc:  # never let resolution crash the daemon
            log.warning("resolve failed for %r: %s", title, exc)
            return None

    # -- search -------------------------------------------------------------
    def _search_workspace(self, root: Path, file_name: str) -> Optional[Path]:
        """
        Pruned recursive search for `file_name` under `root`.

        We use os.walk (not Path.rglob) specifically so we can PRUNE ignored
        directories in-place — rglob would still descend into node_modules and
        .git before filtering, which is exactly the cost we must avoid.

        Duplicate file names across folders are handled by collecting every
        match and choosing the most-recently-modified one — the file you are
        actively editing is the one most recently saved.
        """
        matches: list[Path] = []
        scanned = 0

        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # Prune ignored dirs in place so os.walk never enters them.
            dirnames[:] = [d for d in dirnames if d not in config.IGNORED_DIRS
                           and not d.startswith(".")]

            scanned += len(filenames)
            if scanned > config.MAX_FILES_SCANNED:
                log.warning("scan cap (%d files) hit under %s; stopping early.",
                            config.MAX_FILES_SCANNED, root)
                break

            if file_name in filenames:
                matches.append(Path(dirpath) / file_name)

        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        # Disambiguate duplicates: newest mtime wins.
        def safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        best = max(matches, key=safe_mtime)
        log.info("'%s' matched %d files; chose newest: %s",
                 file_name, len(matches), best)
        return best

    # -- safe read ----------------------------------------------------------
    def _read_text_file(self, path: Path) -> Optional[str]:
        """
        Read file text via the shared guardrails, with an extra mtime cache so
        the polling path (Phase 3.1) doesn't re-read an unchanged file every
        time the user revisits the same window. Returns None when unchanged.
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        if str(path) == self._cache_path and mtime == self._cache_mtime:
            return None  # unchanged since last read -> nothing new to store

        content = read_text_with_guardrails(path)
        if content is None:
            return None

        self._cache_path = str(path)
        self._cache_mtime = mtime
        return content
