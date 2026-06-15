# Nexus AI — Phase 1.1: The Background Window Tracker Daemon

> A learning guide. This document explains, in plain language, **what** we
> built in Phase 1.1, **why** we chose each tool, and **how** the code works
> under the hood. No prior knowledge of the rest of the project is assumed.

---

## 1. What is this phase, in one sentence?

We built the assistant's **eyes**: a small, silent background program that
notices which window you're looking at and writes a clean timeline of your day
to a local database — using almost no CPU and never taking a screenshot.

That's it. No AI, no UI, no internet. Just honest, private note-taking about
*"which app/file did the user have in focus, and when?"*

---

## 2. The big picture (how the pieces fit)

```
                 every 5 seconds
                       │
                       ▼
      ┌──────────────────────────────┐
      │  window_source.py            │   "Which window is focused right now?"
      │  (xdotool + psutil)          │   → WindowSample(app, title, pid)
      └──────────────┬───────────────┘
                     ▼
      ┌──────────────────────────────┐
      │  filters.py                  │   "Is this real work, or OS noise?"
      │                              │   gnome-shell / Desktop / empty → drop
      └──────────────┬───────────────┘
                     ▼
      ┌──────────────────────────────┐
      │  tracker.py  (the daemon)    │   "Did the window CHANGE since last time?"
      │  change-detection + loop     │   yes → log it.  no → skip (keeps log clean)
      └──────────────┬───────────────┘
                     ▼
      ┌──────────────────────────────┐
      │  db.py                       │   append one row to the timeline
      │  (SQLite: local_logs.db)     │
      └──────────────────────────────┘
```

Each file has exactly one job. That separation is deliberate — when Phase 2
swaps the database for a vector store, or Phase 1.2's UI reads the timeline,
only one file needs to change.

---

## 3. Every tool & library — and why

| Tool / Library | What it is | Why we use it here |
|---|---|---|
| **Python 3** | The language. | Cross-platform, batteries-included, great for small daemons and the rest of the AI stack later. |
| **`xdotool`** *(system pkg, Linux/X11)* | A command-line tool that can ask the X11 window server questions. | It's the most reliable way to get the **currently focused window's** ID, title, and owning process ID on Linux without writing C. We call it via `subprocess`. |
| **`subprocess`** *(stdlib)* | Runs other programs from Python. | This is the "shell hook" the spec describes — we shell out to `xdotool` and read its output. |
| **`psutil`** | Cross-platform process/system info. | `xdotool` gives us a **PID** (a number); `psutil` turns that number into a human **application name** (e.g. `code`, `chrome`). Also used later for resource monitoring. |
| **`pygetwindow`** *(Windows only)* | Reads the active window on Windows. | Kept for Windows portability. On Linux it's never imported. |
| **`sqlite3`** *(stdlib)* | A tiny embedded database, built into Python. | Stores the timeline. No server to install, the whole DB is one file (`local_logs.db`), and it's **queryable** — Phase 4's report generator will just `SELECT` from it. |
| **`logging`, `signal`, `time`, `dataclasses`** *(stdlib)* | Standard helpers. | Clean console output, graceful Ctrl-C/SIGTERM shutdown, the polling clock, and a typed `WindowSample` record. |

**Why SQLite over a JSON file?** A JSON file has to be re-read and re-written
in full on every change, it corrupts easily if the process is killed
mid-write, and it's awkward to query. SQLite appends safely, survives crashes,
and lets later phases run real queries like *"what did I work on between 2pm
and 4pm?"*.

**Why no screenshots?** Privacy. The spec is explicit: we log only window
*titles*, never pixels. This keeps CPU near zero and means no sensitive screen
content is ever stored.

---

## 4. The core functionality, step by step

### a) Capture — "what's focused?" (`window_source.py`)
Every tick we run three tiny commands:

```
xdotool getactivewindow          → the window's numeric ID
xdotool getwindowname <id>       → its title text  ("tracker.py - nexus")
xdotool getwindowpid  <id>       → the owning process ID (e.g. 4521)
```

Then `psutil.Process(4521).name()` turns that PID into `"code"`. We bundle the
three facts into an immutable `WindowSample(app_name, title, pid)`.

Every command has a **2-second timeout** so a hung query can never stall the
loop, and every failure returns `None` instead of crashing.

### b) Filter — "is this noise?" (`filters.py`)
The raw stream is full of junk. We drop a sample if:
- its app is a known shell process (`gnome-shell`, `mutter`, …),
- its title is a known non-title (`Desktop`, empty string, gnome internals),
- its title is shorter than the minimum length.

All these rules live as editable sets in `config.py`, so tuning the filter
never means touching logic code.

### c) Change-detection — "is this new?" (`tracker.py`)
This is the trick that keeps the log clean and the CPU cold. We remember the
**last logged window** as a `(app, title)` key:

- If the new sample's key **differs** → it's a real window switch → log it as
  `event="switch"`.
- If it's the **same** window → normally we skip writing (no duplicate spam).
- *Exception:* if the same window has been focused for a long time
  (`HEARTBEAT_SECONDS`, default 5 min), we write one `event="heartbeat"` row so
  the timeline shows "still working here" rather than a suspicious gap.

So switching between VS Code → Chrome → terminal produces exactly three rows,
not one row every five seconds.

### d) Store — "write it down" (`db.py`)
Each kept sample becomes one row in the `activity_log` table:

| column | meaning |
|---|---|
| `id` | auto-increment row number |
| `ts_utc` | ISO-8601 UTC timestamp of the observation |
| `app_name` | resolved application (e.g. `code`) |
| `title` | the window title |
| `pid` | owning process id (nullable) |
| `event` | `'switch'` or `'heartbeat'` |

Reading the rows in `id` order *is* your timeline.

### e) The loop & graceful shutdown (`tracker.py`)
The daemon runs `while self._running:` — capture, filter, decide, maybe store,
then `time.sleep(5)`. Because it sleeps 99.99% of the time and only fires three
cheap subprocess calls per tick, measured CPU is **≈0%** and memory a few MB.

`SIGINT` (Ctrl-C) and `SIGTERM` (`kill`) flip `_running` to `False`, so the
loop finishes its current iteration, **closes the database cleanly**, and exits
without corrupting anything.

---

## 5. How to run, test, and inspect it

```bash
# one-time setup
sudo apt install xdotool
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# run the daemon (Ctrl-C to stop)
python -m tracker.tracker
#   …or:  ./run_tracker.sh

# in another terminal: see your timeline
python -m tracker.inspect_log 30

# run the automated tests
python -m pytest tests/ -v      # or: python tests/test_tracker.py
```

**Manual test (matches the spec):** start the daemon, then switch between your
IDE, a browser tab, and your terminal for ~30 seconds. Run `inspect_log` and
confirm the rows match your exact sequence — and that no `gnome-shell` /
`Desktop` noise appears.

---

## 6. File map for Phase 1.1

```
Nexus AI/
├── tracker/
│   ├── __init__.py          # package marker + version
│   ├── config.py            # all tunables: paths, interval, filter rules
│   ├── window_source.py     # OS-specific "what's focused?"  (xdotool+psutil)
│   ├── filters.py           # noise rejection
│   ├── db.py                # SQLite storage layer
│   ├── tracker.py           # the daemon: loop, change-detection, signals
│   └── inspect_log.py       # CLI to print the timeline
├── data/
│   └── local_logs.db        # the SQLite timeline (auto-created)
├── tests/
│   └── test_tracker.py      # unit tests (no display needed)
├── requirements.txt
├── run_tracker.sh
└── docs/
    └── PHASE_1.1_Window_Tracker.md   # this file
```

---

## 7. Known limits & what Phase 1.2 picks up

- **Wayland:** `xdotool` is X11-only. On a Wayland session you'd add a
  GNOME-Shell/`gdbus` path inside `window_source.py` (the rest is unchanged).
  Our target machine runs X11, so this is deferred.
- **No UI yet:** reading the DB is via `inspect_log.py`. Phase 1.2 adds the
  Tauri/React dark-mode dashboard that reads `local_logs.db` live.
- **Autostart:** running it as a `systemd --user` service (so it launches at
  login and runs truly in the background) is an operational add-on, not part of
  the 1.1 code.
