# Nexus AI — Phase 3.2: File-Change Event Hook (watchdog)

> A learning guide. Plain-language explanation of **what** we built, **why**
> each tool, and **how** the debouncing + thread-safe workspace swapping work.

---

## 1. What is this phase, in one sentence?

We stopped *polling* files and started *listening* for saves: a `watchdog`
observer running on a background thread tells us the instant you press
Ctrl+S, and only then do we re-read the file — so disk I/O happens on save,
not on a timer.

Phase 3.1 found the active file and read it on every window switch. Phase 3.2
keeps that snapshot **fresh as you edit**, with almost zero disk activity.

---

## 2. The big picture (two cooperating threads)

```
  MAIN THREAD (window tracker loop)            OBSERVER THREAD (watchdog)
  ───────────────────────────────             ──────────────────────────
  every window switch:                          OS fires FS events on save
    parse title -> project + file                      │
    resolve absolute path                              ▼
    observer.set_active_context(...) ──────▶  _DebouncedSaveHandler
        │  (if project changed,                  on_modified / on_created
        │   swap watched directory)                    │ trailing-edge
        ▼                                              ▼ debounce (0.5s)
   activity_log + initial snapshot          is this the ACTIVE file?
                                                       │ yes
                                                       ▼
                                            read (≤500KB, non-binary)
                                                       ▼
                                            UPSERT active_file_context
```

Both threads share one `ActivityStore`, whose writes are serialized by a lock,
so they never corrupt each other.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`watchdog`** | Cross-platform filesystem-event library. On Linux it uses the kernel's **inotify**, so the OS notifies us on save instead of us scanning the disk. Near-zero idle cost. |
| **`threading` (stdlib)** | Runs the observer on a background thread and powers the per-path debounce timers. |
| **`threading.Lock`** | Makes the shared active-file state and the SQLite writes thread-safe across the two threads. |
| **stdlib only otherwise** | Reuses the Phase 3.1 guardrails (`read_text_with_guardrails`, `is_path_ignored`) so the rules live in one place. |

---

## 4. The debouncing algorithm (the important part)

**The problem:** one Ctrl+S is *not* one filesystem event. Editors write a temp
file, rename it over the original, touch metadata, update swap files… so a
single save can fire 3–8 events in tens of milliseconds. Reading on each one
would mean redundant disk reads and DB writes — and you might read a
half-written file.

**The solution — trailing-edge debounce, per file path:**

1. Every incoming event for a path **(re)starts a 0.5-second timer** for that
   path. If another event for the same path arrives first, the old timer is
   **cancelled** and a fresh one starts.
2. The read only happens when a path has been **quiet for 0.5 seconds** — i.e.
   the save burst has finished. The timer's callback (`_fire`) runs once.
3. Because each path has its own timer (stored in a dict under a lock), saving
   two different files doesn't interfere.

This collapses an entire burst into **exactly one read**, and crucially reads
*after* the write settles — never mid-write. (Contrast with "leading-edge"
throttling, which reads on the first event and risks a partial file.)

**Verified live:** 5 rapid writes within the window produced **one** stored
read containing the final content (`v5-burst`), not five.

---

## 5. How the background thread safely swaps projects

When you switch from project EMPIRA_HR to Nexus AI, the observer must stop
watching the old tree and start watching the new one — without restarting the
daemon and without a race between the two threads.

1. The main thread calls `set_active_context(root=...)` on every window switch.
2. Inside, it takes `self._state_lock` and compares the new root to
   `self._current_root`. If unchanged, nothing happens (cheap, common case).
3. If the root changed, `_swap_watch_locked()` runs **while still holding the
   lock**:
   - `observer.unschedule(old_watch)` detaches the old inotify watch.
   - `handler.cancel_all()` drops any pending debounce timers from the old tree
     (so a save that was mid-debounce in the old project can't fire after the
     swap).
   - `observer.schedule(handler, new_root, recursive=True)` attaches the new
     watch — **on the same long-lived observer thread**. The thread is never
     stopped or recreated; only its set of watched paths changes.
4. The single `Observer` object lives for the whole session; swapping is just
   add/remove watch operations on it. That's why it's seamless.

Because the swap, the active-file pointer, and the save-handler's read all take
the **same lock**, there's no window where the observer reads the new file
using the old project's metadata, or vice-versa.

**Verified live:** after swapping ProjA→ProjB, a save in ProjB was captured,
and a later save to ProjA's old file was correctly **ignored**.

---

## 6. Guardrails & defensive error handling

| Concern | How it's handled |
|---|---|
| Editor swap files / bursts | trailing-edge debounce (one read per save) |
| Ignored trees (`node_modules`, `.git`, `venv`) | `is_path_ignored()` rejects before any disk read — verified a `node_modules/views.py` save is ignored |
| Binary / lock / >500KB files | shared `read_text_with_guardrails()` returns None |
| Only the active file matters | handler compares `path.resolve()` to the active file; other saves in the tree are skipped |
| Locked / unreadable file | `read_bytes()` wrapped in try/except → returns None, daemon continues |
| A crashing callback | `_fire()` wraps the callback in try/except so the observer thread survives |
| Cross-thread DB access | `ActivityStore` opens with `check_same_thread=False` and serializes every write with a `Lock` |
| Clean shutdown | `observer.stop()` + `join(timeout=3)` and `cancel_all()` on SIGINT/SIGTERM |

---

## 7. Files in this phase

```
tracker/
├── workspace_observer.py   # NEW — WorkspaceObserver + _DebouncedSaveHandler
├── file_resolver.py        # UPDATED — extracted shared is_path_ignored() and
│                           #           read_text_with_guardrails(); parse()/root_for()
├── db.py                   # UPDATED — thread-safe (check_same_thread=False + Lock)
├── config.py               # UPDATED — OBSERVER_DEBOUNCE_SECONDS = 0.5
└── tracker.py              # UPDATED — starts/stops observer; feeds it active context
requirements.txt            # UPDATED — added watchdog>=4.0
tests/test_tracker.py       # parsing/duplicate/upsert tests (15 pass)
```

---

## 8. How to run

```bash
pip install -r requirements.txt        # now includes watchdog
./run_tracker.sh                       # or: python -m tracker.tracker
```

Open a file in a mapped project (see `workspace_config.json`), edit it, press
Ctrl+S. You'll see a single line per save:

```
save -> ctx  views.py | /home/empiric/Projects/EMPIRA_HR/backend/views.py (1423 chars)
```

Confirm the content refreshes on save:

```bash
sqlite3 data/local_logs.db \
  "SELECT file_name, ts_utc, length(file_content) FROM active_file_context;"
```

---

## 9. What's next

The `active_file_context` table now stays live as you type. Phase 2.2 can feed
that fresh `file_content` straight into the Ollama chat so the AI debugs the
exact code on your screen; Phase 2.3 embeds it into ChromaDB for recall.
