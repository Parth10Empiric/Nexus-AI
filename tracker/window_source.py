"""
OS-specific "what window is focused right now?" capture.

The roadmap mentions `pygetwindow` for Windows and a shell-hook + `psutil`
approach for Linux. Our execution environment is Linux/Ubuntu on X11, so the
Linux path is the real implementation. We isolate every OS-specific detail in
this one file: the rest of the daemon just calls `get_active_window()` and
receives a clean `WindowSample` regardless of platform.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass(frozen=True)
class WindowSample:
    """A single observation of the focused window."""
    app_name: str
    title: str
    pid: Optional[int]


def _run(cmd: list[str]) -> Optional[str]:
    """Run a short shell command and return stripped stdout, or None on error."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2,  # never let a hung command stall the 5s loop
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


class _LinuxX11Source:
    """
    Reads the active window via `xdotool` (X11) and resolves the owning
    process name via `psutil`. This is the `subprocess` + `psutil` hook the
    spec describes.
    """

    def __init__(self) -> None:
        if shutil.which("xdotool") is None:
            raise RuntimeError(
                "xdotool not found. Install it with: sudo apt install xdotool"
            )

    def get_active_window(self) -> Optional[WindowSample]:
        win_id = _run(["xdotool", "getactivewindow"])
        if not win_id:
            return None  # e.g. focus is on the root window / no window focused

        title = _run(["xdotool", "getwindowname", win_id]) or ""

        pid_str = _run(["xdotool", "getwindowpid", win_id])
        pid = int(pid_str) if pid_str and pid_str.isdigit() else None

        app_name = self._app_name_from_pid(pid)
        return WindowSample(app_name=app_name, title=title, pid=pid)

    @staticmethod
    def _app_name_from_pid(pid: Optional[int]) -> str:
        if pid is None:
            return "unknown"
        try:
            return psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return "unknown"


class _WindowsSource:
    """Windows implementation using pygetwindow + psutil (per the spec)."""

    def __init__(self) -> None:
        import pygetwindow  # imported lazily so Linux never needs it
        self._pgw = pygetwindow

    def get_active_window(self) -> Optional[WindowSample]:
        win = self._pgw.getActiveWindow()
        if win is None or not win.title:
            return None
        # pygetwindow doesn't expose the PID directly; app name = title's owner
        # is left as "unknown" here since this project targets Linux.
        return WindowSample(app_name="unknown", title=win.title, pid=None)


def build_window_source():
    """Factory: return the right window source for the current OS."""
    if sys.platform.startswith("linux"):
        return _LinuxX11Source()
    if sys.platform.startswith("win"):
        return _WindowsSource()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
