# Phase 7.2 — Dual-Database Storage Layer (Postgres + per-tenant ChromaDB)

**Goal:** deprecate the local SQLite3 storage and build a multi-tenant storage
layer with **two backends**, isolated per user:

- **PostgreSQL** (SQLAlchemy) — relational/audit data (sessions, window logs,
  file history).
- **ChromaDB** — vector memory, with a **separate collection pair per user**.

Every relational row and every vector collection is scoped by `username`, so one
tenant can never see another's data.

## Files

| File | Role |
|------|------|
| `database.py` | Pooled SQLAlchemy engine + safe connection string + session scope |
| `models.py` | `UserSession`, `WindowLog`, `FileTracking` — all carry `username` |
| `memory_manager.py` | `TenantMemoryManager.get_user_vaults(username)` → per-user Chroma collections |
| `data_service.py` | `process_incoming_file_sync()` — routes one file to BOTH databases |

## Key design points

### Password URL-encoding (the `@` fix)
The password `Postgres@1011` contains `@`, which is the URL userinfo delimiter.
`database.py` runs it through `urllib.parse.quote_plus` →
`Postgres%401011`, producing:
```
postgresql+psycopg2://postgres:Postgres%401011@localhost:5432/postgres
```
Without this the URL parses `1011@localhost` as the host and fails.

### Engine
`create_engine(..., pool_size=20, max_overflow=0, pool_pre_ping=True)` — a fixed
pool of 20, no overflow (fail fast under overload), with dead-connection recycle.

### Multi-tenant Chroma
No global collection. `get_user_vaults("friend_a")` lazily creates and caches:
```
friend_a_codebase_vault   friend_a_activity_vault
friend_b_codebase_vault   friend_b_activity_vault
```
Isolation is structural — a query for `friend_a` can only touch `friend_a_*`.

### Data router
`process_incoming_file_sync(username, file_path, file_content)`:
1. Dedupe: skip if this user+path+content-hash is already logged.
2. **Postgres** — insert the raw text into `file_tracking` (audit/history).
3. **ChromaDB** — chunk (`RecursiveCharacterTextSplitter`, 2000/200 chars) →
   embed (local Ollama `nomic-embed-text`) → upsert into the user's
   `{username}_codebase_vault`, replacing any prior chunks for that path.

## How to run / set up

### 1. Install dependencies
```bash
cd "/home/empiric/Projects/Nexus AI" && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Have PostgreSQL running with the expected credentials
Defaults (override via env): user `postgres`, password `Postgres@1011`, db
`postgres`, host `localhost:5432`. Env overrides:
`NEXUS_PG_USER / NEXUS_PG_PASSWORD / NEXUS_PG_HOST / NEXUS_PG_PORT / NEXUS_PG_DB`.

```bash
# quick local Postgres via Docker (optional):
docker run -d --name nexus-pg -e POSTGRES_PASSWORD='Postgres@1011' -p 5432:5432 postgres:16
```

### 3. Create the schema
```bash
python database.py        # runs init_db() → CREATE TABLE IF NOT EXISTS ...
```

### 4. Ensure Ollama + the embed model (for the vector half)
```bash
ollama pull nomic-embed-text     # data_service embeds via http://localhost:11434
```

## How to test

### A. Verify the connection string + schema + tenant isolation (no Postgres needed)
```bash
python - <<'PY'
import database, models
print("encoded pw in URL:", "Postgres%401011" in database.DATABASE_URL)
print("tables:", sorted(database.Base.metadata.tables))
print("all have username:", all("username" in t.c for t in database.Base.metadata.tables.values()))
from memory_manager import get_memory
m = get_memory()
a, _ = m.get_user_vaults("friend_a"); b, _ = m.get_user_vaults("friend_b")
print("isolated vaults:", a.name, "!=", b.name, "->", a.name != b.name)
PY
```
Expect: encoded pw True, the three tables, all-have-username True, isolated True.

### B. End-to-end round trip (Postgres + Ollama running)
```bash
python - <<'PY'
from data_service import process_incoming_file_sync
r = process_incoming_file_sync("friend_a", "/tmp/demo.py", "def hi():\n    return 'hello'\n")
print(r)   # {'saved': True, 'chunks': 1, 'skipped': False}
# re-run with identical content -> dedupe
print(process_incoming_file_sync("friend_a", "/tmp/demo.py", "def hi():\n    return 'hello'\n"))
# -> {'saved': False, 'chunks': 0, 'skipped': True}
PY
```
Then confirm the row landed in Postgres:
```sql
SELECT username, file_name, chunk_count FROM file_tracking WHERE username='friend_a';
```
…and the chunk landed only in `friend_a_codebase_vault` (not friend_b's).

## Next (Phase 7.3 and beyond)

- Wire `process_incoming_file_sync` into the Phase 7.1 `/ws` loop (run it in a
  thread/executor) so client file saves persist server-side per tenant.
- Phase 7.3 replaces the hardcoded `VALID_USERS` invite keys with a Postgres
  `users` table (the relational core is now in place for it).
