# Phase 7.1 — Central "Brain" Server (WebSocket gateway + Invite-Key auth)

**Goal:** stand up the central FastAPI server that remote React/Tauri clients
connect to over WebSocket. Each connection authenticates with an **Invite Key**
and is mapped to a username in an in-memory registry. This is the foundation of
the Phase 7 Client-Server SaaS migration.

In scope: the **auth handshake**, the **ConnectionManager** (who's online +
isolated per-user routing), and the **`/ws`** endpoint lifecycle.
Out of scope (later phases): the LLM/voice/memory pipeline, and a Postgres user
store (Phase 7.3 replaces the hardcoded `VALID_USERS`).

## What was built

| Piece | Location | Role |
|-------|----------|------|
| Server | `server.py` | FastAPI app, run on port 8000 |
| Deps | `requirements.txt` | `fastapi`, `uvicorn[standard]`, `pydantic` |

## The auth handshake

```
client                              server (/ws)
  │  ── WebSocket upgrade ──────────▶ accept()
  │  ── {"type":"auth",              ┌ wait ≤10s for FIRST frame
  │      "invite_key":"nexus_key_44bB"} ─▶ validate shape (pydantic)
  │                                  │ VALID_USERS[key] -> username
  │  ◀─ {"type":"auth_ok", ──────────┘ manager.connect(ws, username)
  │      "username":"friend_a"}
  │  ── {...any json...} ───────────▶ (echoed back this phase)
  │  ◀─ {"type":"echo", "received":{...}}
```

If the first frame is missing / not `type:"auth"` / has an unknown key, or no
frame arrives within **10s**, the socket is closed with **1008 Policy Violation**.

## ConnectionManager

```python
ACTIVE_CONNECTIONS = { "friend_a": {"websocket": <WebSocket>, "status": "active"} }
```

- `connect(ws, username)` — registers an already-authenticated socket. A second
  login for the same user evicts the previous socket (one live socket per user).
- `disconnect(username)` — idempotent removal; runs in the endpoint's `finally`
  so a dropped client never leaves a ghost entry.
- `send_personal_message(message, username)` — **isolated** delivery to exactly
  one user (per-user lock serialises writes; a dead socket is auto-evicted).
  There is deliberately no broadcast in this phase.

## Mock users (baseline — replaced by Postgres in 7.3)

| Invite Key | Username |
|------------|----------|
| `nexus_key_44bB` | `friend_a` |
| `nexus_key_99xA` | `friend_b` |

## How to run

### 1. Install dependencies
```bash
cd "/home/empiric/Projects/Nexus AI"
source venv/bin/activate          # or your venv
pip install -r requirements.txt
```

### 2. Start the server
```bash
python server.py
# serves http://0.0.0.0:8000  (health) and ws://0.0.0.0:8000/ws
# auto-reload is on (uvicorn reload=True)
```

### 3. Expose it to remote clients via ngrok
```bash
ngrok http 8000
```
ngrok prints a public URL like `https://abc123.ngrok-free.app`. Remote
React/Tauri clients connect to the WebSocket as:
```
wss://abc123.ngrok-free.app/ws
```
(`https` → `wss`; ngrok upgrades WebSockets automatically.)

## How to test

### A. Health check
```bash
curl http://localhost:8000/
# {"service":"nexus-brain","phase":"7.1","online_users":0}
```

### B. Quick automated handshake test (no client needed)
With deps installed, run this from the project root — it exercises every path:
```bash
python - <<'PY'
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import server
c = TestClient(server.app)

# valid key -> auth_ok + echo round-trip
with c.websocket_connect("/ws") as ws:
    ws.send_json({"type": "auth", "invite_key": "nexus_key_44bB"})
    print("auth:", ws.receive_json())
    ws.send_json({"hello": "world"})
    print("echo:", ws.receive_json())

# invalid key -> closed 1008
try:
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "invite_key": "WRONG"})
        ws.receive_json()
except WebSocketDisconnect as e:
    print("invalid key closed with:", e.code)   # 1008
PY
```
Expected: `auth_ok` then an `echo`, then `invalid key closed with: 1008`.

### C. Browser console (point at local or ngrok URL)
```js
const ws = new WebSocket("ws://localhost:8000/ws");
ws.onmessage = (e) => console.log("◀", JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({ type: "auth", invite_key: "nexus_key_44bB" }));
// after auth_ok:
ws.send(JSON.stringify({ ping: 1 }));   // -> {type:"echo", received:{ping:1}}
```

### D. Failure cases to confirm
- Wrong key → socket closes with code **1008**.
- First frame not `{"type":"auth",...}` → **1008**.
- Send nothing for 10s after connecting → server closes (**auth timeout**).

## Next (Phase 7.x)

- Replace the echo in the `/ws` listen loop with real LLM/voice routing.
- Phase 7.3: swap `VALID_USERS` for a Postgres-backed user/key store.
