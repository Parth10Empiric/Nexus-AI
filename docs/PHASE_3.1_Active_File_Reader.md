# Nexus AI — Phase 3.1: Active File Source Reader

> A learning guide. Plain-language explanation of **what** we built, **why**
> each tool, and **how** the title parsing, file search, and storage work.

---

## 1. What is this phase, in one sentence?

We upgraded the tracker from knowing *the title* of your window to knowing the
*actual source code* inside it: it parses `views.py - EMPIRA_HR - VS Code`,
finds that file on disk, reads its text, and stores it so the AI can reason
about the real code you're editing — never a guess from the title alone.

---

## 2. The pipeline

```
window title  "● views.py - EMPIRA_HR - Visual Studio Code"
      │
      ▼  parse_title()  (regex)
  file_name="views.py"   project_keyword="EMPIRA_HR"
      │
      ▼  workspace_config.json lookup
  root = /home/empiric/Projects/EMPIRA_HR
      │
      ▼  _search_workspace()  (pruned os.walk, skips node_modules/.git/venv)
  /home/empiric/Projects/EMPIRA_HR/backend/views.py   (newest if duplicates)
      │
      ▼  _read_text_file()  (≤500KB, binary-sniff, utf-8 errors='replace')
  raw source text
      │
      ▼  active_file_context table  (UPSERT on absolute_path)
  [timestamp, window_title, app_name, file_name, absolute_path, file_content]
```

It only runs on a real **window switch** (not heartbeats), so CPU stays `<1%`.

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **`re` (stdlib)** | Regex to pull the file name and project keyword out of messy editor titles. Zero dependency. |
| **`os.walk` (stdlib)** | Recursive directory scan that lets us **prune** ignored folders *before* descending — far faster than `pathlib.rglob`, which would walk into `node_modules` then filter. |
| **`pathlib.Path` (stdlib)** | Clean path joining, `stat()`, `read_bytes()`. |
| **`json` (stdlib)** | Loads `workspace_config.json` (the keyword → absolute-path map). |
| **`sqlite3` (stdlib)** | New `active_file_context` table with an UPSERT so each file has one always-current row. |

Everything is standard library — no new pip installs, no overhead.

---

## 4. The regex extraction logic (the important part)

Editor titles are inconsistent: `views.py - EMPIRA_HR - Visual Studio Code`,
`● tracker.py - Nexus AI — Cursor`, `main.go - VS Code`. We handle all of them:

1. **Strip the "dirty" marker.** `_DIRTY_MARKER = ^[\s•\*●◆]+` removes the
   bullet/asterisk editors prepend to unsaved files.
2. **Split into segments** on ` - `, ` — `, or ` | ` (`_SEGMENT_SPLIT`).
3. **Find the file name** with `_FILENAME_RE = ([\w.\-+#]+\.[A-Za-z0-9]{1,8})`.
   It requires a dot followed by a 1–8 char extension, so words like "Visual
   Studio Code" are never mistaken for files. We check the **leftmost** segment
   first because editors put the file there.
4. **Find the project keyword** by scanning the whole title for any keyword in
   `workspace_config.json` (case-insensitive). This is order-independent and
   robust across editors.

If no file name is found (a browser tab) or no known project keyword is present
(an unmapped file), we return `None` and skip — exactly what we want.

**Verified:** `views.py - EMPIRA_HR - VS Code` → `(views.py, EMPIRA_HR)`;
`ReactJS … - YouTube - Chrome` → `None`.

---

## 5. How duplicate file names are handled safely

`views.py` may exist in a dozen folders. The search **collects every match**,
then disambiguates:

- **Ignored folders are pruned first.** `dirnames[:] = [...]` mutates `os.walk`'s
  list in place so it never even enters `node_modules`, `.git`, `venv`, dot-dirs,
  `dist`, `build`, etc. This both speeds the scan and removes false matches (a
  vendored `views.py` deep in `node_modules` can't win).
- **Newest-modified wins.** Among the remaining matches we pick `max(st_mtime)`.
  The file you're actively editing is the one most recently saved, so this is a
  reliable, cheap heuristic — no fragile path guessing.
- **A scan cap** (`MAX_FILES_SCANNED`, 20k) guarantees an enormous workspace can
  never make one tick run long; it logs and stops early if exceeded.

**Verified:** with `a/views.py`, `b/views.py`, and `b/node_modules/views.py`,
the resolver pruned `node_modules` and returned `b/views.py` (the newest).

---

## 6. Guardrails (performance & safety)

| Guardrail | Where | Effect |
|---|---|---|
| Prune `node_modules`, `.git`, `venv`, `dist`, dot-dirs… | `IGNORED_DIRS` | scan stays fast, no junk matches |
| Skip binary/lock extensions | `IGNORED_FILE_EXTENSIONS` | never read `.png`, `.lock`, `.db`, … |
| 500 KB size limit | `MAX_FILE_SIZE_BYTES` | huge files can't stall the loop |
| Null-byte sniff | `_read_text_file` | binary content rejected even with a text-y extension |
| `errors="replace"` decode | `_read_text_file` | one bad byte can't crash the daemon |
| mtime cache | `FileResolver` | unchanged file isn't re-read/re-stored every switch |
| 20k file scan cap | `MAX_FILES_SCANNED` | bounded worst-case work per tick |
| `try/except` around `resolve()` | `FileResolver.resolve` | resolution failure never kills tracking |

---

## 7. Database change

New table in `local_logs.db` (the `activity_log` table is unchanged):

```sql
CREATE TABLE active_file_context (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT NOT NULL,
    window_title  TEXT NOT NULL,
    app_name      TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    absolute_path TEXT NOT NULL UNIQUE,   -- one row per file
    file_content  TEXT NOT NULL
);
```

`save_file_context()` uses `INSERT … ON CONFLICT(absolute_path) DO UPDATE`, so a
file is **inserted once and overwritten thereafter** — the table is always the
latest snapshot of each file, never an ever-growing history.

---

## 8. Files in this phase

```
tracker/
├── config.py            # UPDATED — workspace map path, IGNORED_DIRS,
│                        #           IGNORED_FILE_EXTENSIONS, size/scan caps
├── file_resolver.py     # NEW — parse_title() + FileResolver (search + read)
├── db.py                # UPDATED — active_file_context table + UPSERT methods
└── tracker.py           # UPDATED — calls resolver on each window 'switch'
workspace_config.json    # NEW — keyword -> absolute-path map (edit me!)
tests/test_tracker.py    # UPDATED — parsing, duplicate, upsert tests
```

---

## 9. How to run

```bash
# 1. Edit workspace_config.json so the keywords match YOUR projects:
#    "EMPIRA_HR": "/home/empiric/Projects/EMPIRA_HR"
# 2. Run the daemon as before:
./run_tracker.sh          # or: python -m tracker.tracker
# 3. Open a file in VS Code inside a mapped project; on switch you'll see:
#    file ctx  views.py | /home/empiric/Projects/EMPIRA_HR/backend/views.py (1234 chars)
```

Inspect what was captured:

```bash
sqlite3 data/local_logs.db \
  "SELECT file_name, absolute_path, length(file_content) FROM active_file_context;"
```

---

## 10. What's next

The AI brain (Phase 2.1) currently injects the window *title*. With this table
in place, the next step is to inject the actual `file_content` for the active
file — giving the assistant the real code to debug, not just the file name.
