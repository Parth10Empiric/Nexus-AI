"""
Noise filtering for window samples.

The raw stream of focused windows includes a lot of junk: the GNOME shell
itself, the desktop background, empty-titled transient popups. This module
decides whether a given sample is "meaningful developer activity" worth
keeping.
"""

from __future__ import annotations

from . import config
from .window_source import WindowSample


def is_meaningful(sample: WindowSample) -> bool:
    """Return True if this window sample should be logged, False if it's noise."""
    app = (sample.app_name or "").strip().lower()
    title = (sample.title or "").strip()

    if app in config.IGNORED_APP_NAMES:
        return False

    if title.lower() in config.IGNORED_TITLES:
        return False

    if len(title) < config.MIN_TITLE_LENGTH:
        return False

    return True
