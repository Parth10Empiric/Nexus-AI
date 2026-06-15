"""
Tiny helper to eyeball the timeline without opening a SQLite client.

Usage:  python -m tracker.inspect_log [N]
        (N = how many recent rows to show, default 20)
"""

import sys

from . import config
from .db import ActivityStore


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    store = ActivityStore(config.DB_PATH)
    rows = store.recent(limit)
    store.close()

    if not rows:
        print("No entries yet. Is the tracker running?")
        return

    print(f"{'time (UTC)':<28} {'event':<10} {'app':<18} title")
    print("-" * 90)
    for r in reversed(rows):  # oldest first = natural timeline
        print(f"{r['ts_utc']:<28} {r['event']:<10} {r['app_name']:<18} {r['title']}")


if __name__ == "__main__":
    main()
