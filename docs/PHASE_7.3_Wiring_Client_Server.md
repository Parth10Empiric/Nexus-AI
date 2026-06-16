# Phase 7.3 — Wiring the Client to the Server (end-to-end SaaS)

**Goal:** connect the disconnected pieces from 6.x/7.1/7.2 into one working
Client-Server flow. After this phase a remote client can open the Tauri app,
authenticate with an invite key, and chat with the brain running on your PC —
with each user's files stored per-tenant.

This phase did the three jobs that were outstanding:

| Job | File(s) | Status |
|-----|---------|--------|
| 1. Frontend → server + auth | `frontend/src/hooks/useAgentSocket.js`, `frontend/.env.example` | ✅ done |
| 2. Server grows a brain | `server.py`, `brain_service.py` | ✅ done (text + file sync) |
| 3. Audio transport | `server.py` (voice-in), `brain_service.transcribe_pcm16` | ⚠️ voice-IN done; voice-OUT (TTS to client) is the remaining gap |

## Job 1 — Frontend points at the server

`useAgentSocket.js` is now env-driven with two modes:

- **Local mode** (default, nothing set): connects to `ws://127.0.0.1:8765`, no
  auth — identical to before. Local dev is unaffected.
- **SaaS mode** (`VITE_NEXUS_INVITE_KEY` set): connects to `VITE_NEXUS_WS_URL`
  (your ngrok `wss://…/ws`) and sends `{"type":"auth","invite_key":"…"}` as the
  **first frame**. Only marks `connected` after the server replies `auth_ok`. A
  `1008` close surfaces the reason in `detail`.

New hook helpers: `syncFile(path, content)`, `sendAudioChunk(int16)`, `endAudio()`.

Configure a client by copying `frontend/.env.example` → `frontend/.env`.

## Job 2 — Server brain (`server.py` + `brain_service.py`)

The `/ws` echo loop is replaced with a real dispatcher that **preserves the
existing frontend protocol** (so the chat UI/voice panel work unchanged):

Incoming → handled:
- `{"cmd":"ask","text"}` → per-tenant retrieve + stream LLM
- `{"cmd":"activate"|"deactivate"|"sleep"|"interrupt"}` → state updates
- `{"type":"file_sync","file_path","file_content"}` → `process_incoming_file_sync` (7.2)
- binary frames → accumulated PCM; `{"type":"audio_end"}` → STT → ask

Outgoing (unchanged protocol): `{"type":"user"}`, `{"type":"token"}`,
`{"type":"answer"}`, `{"type":"state",...}`, plus `{"type":"auth_ok"}`.

`brain_service.py`:
- `retrieve_user_context(username, q)` — embeds `q`, queries **only**
  `{username}_codebase_vault`, drops hits past `RAG_MAX_DISTANCE`, builds the prompt.
- `stream_chat(messages)` — async generator streaming Ollama `/api/chat` tokens
  without blocking the event loop (worker thread + asyncio queue).
- `transcribe_pcm16(pcm)` — wraps raw 16kHz mono Int16 PCM as WAV → faster-whisper.

LLM/RAG settings mirror `tracker/config.py` (`qwen2.5-coder:1.5b`,
`nomic-embed-text`, top-k 3, distance 0.55) and are env-overridable.

## Job 3 — Audio: what works and what doesn't

- ✅ **Voice-IN**: the client streams Phase 6.2 mic chunks (binary) to the
  server; on `audio_end` the server transcribes and runs it as an ask. You speak,
  you get a **text** answer streamed back.
- ⚠️ **Voice-OUT (the gap)**: the legacy `tracker/tts_engine.py` plays audio on
  the **server's** local speakers via `sounddevice` — meaningless for a remote
  client. Speaking the answer on the *client* needs a new browser audio-playback
  subsystem (synthesize WAV server-side → stream bytes → play via a Web Audio
  worklet on the client). That subsystem does not exist yet and is the next phase.

So today: **text chat and voice-in-text-out work end-to-end; spoken replies do not.**

## How to run the full SaaS flow

### On YOUR PC (the host / brain)
```bash
cd "/home/empiric/Projects/Nexus AI" && source venv/bin/activate
pip install -r requirements.txt

# Prereqs for the brain:
ollama pull qwen2.5-coder:1.5b      # chat model
ollama pull nomic-embed-text        # embeddings
python database.py                  # create Postgres tables (Postgres must be up)

python server.py                    # serves :8000
ngrok http 8000                     # → public wss URL
```

### On the CLIENT (your PC for testing, or a friend's)
```bash
cd frontend
cp .env.example .env
# edit .env: set VITE_NEXUS_WS_URL=wss://<your-ngrok>.ngrok-free.app/ws
#            set VITE_NEXUS_INVITE_KEY=nexus_key_44bB
npm run tauri:dev        # or: npm run tauri:build → install the .deb
```
The client connects out to your ngrok URL and authenticates. No Python/ngrok on
the client side.

## How to test

### A. Server wiring without Ollama (stubbed LLM)
```bash
python - <<'PY'
import brain_service, server
from starlette.testclient import TestClient
async def fake_stream(m):
    for t in ["Hi", " there"]: yield t
async def fake_retrieve(u,q): return q
server.brain_service.stream_chat = fake_stream
server.brain_service.retrieve_user_context = fake_retrieve
c = TestClient(server.app)
with c.websocket_connect("/ws") as ws:
    ws.send_json({"type":"auth","invite_key":"nexus_key_44bB"})
    print(ws.receive_json())            # auth_ok
    print(ws.receive_json())            # state sleeping
    ws.send_json({"cmd":"ask","text":"hello"})
    for _ in range(6): print(ws.receive_json())  # user, thinking, tokens, answer
PY
```

### B. Real end-to-end
With Ollama + Postgres up and the client `.env` pointed at ngrok: open the app,
confirm it shows connected, type a question → tokens stream back. Then save a
file via `syncFile(...)` and confirm a row appears in `file_tracking` for your
username and chunks in your `{username}_codebase_vault`.

## Desktop-app troubleshooting (fixes applied)

Three issues seen when running the built/dev Tauri app in SaaS mode, and their fixes:

1. **"Voice Agent · backend offline" / toggle won't turn on.**
   Cause: the Tauri **CSP `connect-src`** didn't allow the ngrok WebSocket, so the
   socket never connected (`connected` stays false → toggle is disabled).
   Fix: `tauri.conf.json` CSP now allows `ws://127.0.0.1:8765` and
   `wss://*.ngrok-free.app|.dev`, `wss://*.ngrok.app|.io` (+ https). **Requires a
   rebuild** — CSP is baked at build time.

2. **Microphone permission denied.**
   Cause: on Linux, WebKitGTK disables `getUserMedia` and auto-denies the
   permission request. Fix: `main.rs` `enable_webview_media()` turns on
   `enable_media_stream` and grants permission requests for the main + orb
   windows (Linux-only; pinned `webkit2gtk = "=2.0.2"` to match wry). **Requires
   a Rust rebuild.**

3. **Active Window shows random/stale names.**
   Cause: the card read the deprecated Python tracker's SQLite DB
   (`get_active_window`) and fell back to a mock when it was absent. Fix:
   `useActiveWindow.js` now subscribes to the Phase 6.1 native
   `nexus://os-context` event — live, no Python tracker needed.

**After these fixes you MUST rebuild** (`npm run tauri:dev` or `npm run tauri:build`);
a frontend-only hot reload won't pick up the CSP or Rust changes.

**Also check your client `.env`** (not `.env.example`): set the REAL ngrok URL in
`wss://…/ws` form, e.g. `VITE_NEXUS_WS_URL=wss://meggan-….ngrok-free.dev/ws`
(note `wss://` not `https://`, and the trailing `/ws`). The `abc123` placeholder
will never connect. If using ngrok's free tier and the WS still fails, the
browser-warning interstitial may be intercepting — verify the raw URL connects.

## Toggle / multi-socket / voice fixes (applied)

- **Toggle stuck on "connecting…" / dead toggle.** Root cause: the frontend
  opens a socket PER component (VoiceAgentPanel, ChatPanel, AgentOrb), all as the
  same user, and the server **evicted duplicate usernames** → an endless
  reconnect war. Fix: `ConnectionManager` now keeps a **set of sockets per user
  and broadcasts** to all of them (no eviction). Plus the state model is now
  off ↔ listening (activate/deactivate emit a real change), and the panel has a
  4s pending-timeout safety net so "connecting…" can never stick.
- **Server states**: auth → `off`; `activate` → `listening`; `deactivate` →
  `off`; `sleep` → `sleeping`; voice answer → back to `listening` (loops).
  `auth_ok` + initial state are sent only to the connecting socket (opening a new
  window no longer resets the others).

## Voice-in: is my audio reaching the server?

Yes — now wired. `useVoiceStream` (in VoiceAgentPanel, main window) streams the
mic to the server while `agentState === "listening"`:

1. Arm the Voice Agent toggle → state `listening` → mic starts.
2. Energy-based VAD: once you speak, Int16 PCM chunks stream as binary frames;
   ~1s of silence fires `audio_end`.
3. Server transcribes (faster-whisper) → answers (streamed text) → returns to
   `listening` for the next utterance.

**There is NO wake word in the SaaS client.** `openwakeword` was a local Python
engine; the browser/Tauri client has none. Saying "hey jarvis" does nothing —
instead, **arm the toggle and just speak**.

## Voice-out (spoken replies) — now implemented

After a voice answer the server synthesizes speech with Piper
(`brain_service.synthesize_tts`, the existing `en_US-lessac-medium.onnx` voice,
22050 Hz mono Int16), base64-encodes the PCM, and sends
`{"type":"tts_audio","sample_rate","pcm_b64"}`. The client plays it via Web Audio
(`useTtsPlayback`, wired in VoiceAgentPanel only, so no double audio across
windows). Web Audio is used rather than the Web Speech API because the latter is
unsupported in WebKitGTK.

Voice loop now: listening → (speak) → thinking → tokens/answer → **speaking
(audio plays)** → listening. The mic is paused while speaking, so the agent
doesn't hear itself.

> Harmless log noise: `GstAppSrc has no property 'automatic-eos'` is a WebKitGTK
> GStreamer version-mismatch warning from the webview's media pipeline — ignore it.

Mic errors now surface in the Voice Agent panel status line (e.g. "mic:
Microphone permission denied.").

## Voice conversation bug-fixes (round 2)

- **Agent replied when nobody spoke / out-of-order "old" replies.** Cause: the
  client kept the mic open in the gap between sending an utterance and the server
  replying, capturing stray noise → extra (often hallucinated) questions that
  stacked up. Fixes: (a) `useVoiceStream` now LATCHES after end-of-utterance and
  ignores audio until the next "listening" turn; (b) requires ≥3 speech chunks
  before ending an utterance; (c) the server ignores utterances < ~0.4s and empty
  transcripts.
- **Agent auto-restarted after being stopped.** Cause: the server always
  re-armed to "listening" after a turn. Fix: a per-connection `armed` flag —
  `activate` sets it, `deactivate`/`sleep` clear it, and a turn only returns to
  "listening" if still armed (else → off). Audio frames are buffered only while
  armed. Turning the agent off also hard-stops any reply still playing
  (`useTtsPlayback.stop()`).
- **Terminal logging.** The server now prints, tagged by username:
  `🗣️ [user] ASK: …`, `🤖 [user] REPLY: …`, `🔊 [user] speaking …`.
- Exact voice sequence now: `listening → thinking → user → token… → answer →
  speaking (audio) → listening` (single `thinking`, no duplicates).

## Echo loop + socket multiplicity fixes (round 3)

- **Agent talked to itself / replied without being asked / "spoke twice".** Root
  cause: an **acoustic feedback loop** — the server flips to `listening` the
  instant it *sends* the audio, but the client is still *playing* it through the
  speakers, so the mic captured the agent's own voice and re-asked it. Fix: the
  client now mutes the mic **while speech is playing** plus a 600ms cooldown
  (`useTtsPlayback` exposes `isPlaying`; `VoiceAgentPanel` gates
  `micActive = listening && !isPlaying && !cooldown`).
- **One client showed up as ~10 connections.** Root cause: every component
  (`ChatPanel`, `VoiceAgentPanel`, `AgentOrb`) opened its OWN socket, multiplied
  by React Strict Mode's double-mount and dev hot-reloads. Fix: a single
  **`NexusSocketProvider`** owns ONE WebSocket per window; all components consume
  it via `useNexusSocket()`. Strict Mode removed (it double-opened the socket in
  dev), and the provider closes its socket cleanly on unmount. Result: **1 socket
  for the main window, +1 only while the orb is open** (the orb is a separate
  window, so it needs its own).

## Screen/file awareness (round 4) — the agent can see your open file

Locally the Python tracker fed the active file to the LLM; in the SaaS server
that was never wired, so the agent was blind. Now wired end-to-end:

- **Client** (`useOsContextSync`, main window): calls `start_file_watcher` on
  `VITE_NEXUS_WORKSPACE`, and forwards every Phase 6.1 `nexus://os-context` event
  to the server as `{type:"os_context", window_title, app_name, file_name,
  file_content}`. Window-only updates are throttled (2s); file content always
  goes through.
- **Server**: keeps the latest `active_ctx` per connection and **injects the
  currently open file into every prompt** (`brain_service.retrieve_user_context`
  prepends an `[ACTIVE SCREEN] currently open file … Its full contents:` block,
  ≤6000 chars). Any file content received is also embedded into the user's
  `{username}_codebase_vault` (`process_incoming_file_sync`).
- **Initial index** (Rust `scan_and_emit`): when the watcher starts it does a
  one-time recursive scan of the workspace, emitting each code/text file for
  embedding — so "explain file X" works even before you save it. Bounded to 300
  files; skips hidden/junk dirs (`node_modules`, `.git`, `venv`, `target`, …) and
  non-text/oversized files.

Now works: "what's the currently open file?", "explain the first three lines of
this file", and questions about other workspace files (via vault retrieval).

**Requires** `VITE_NEXUS_WORKSPACE` set in the client `.env`, Postgres up (for
the file_tracking writes), and Ollama (embeddings). The active-file *prompt
injection* works even without Postgres; only the embedding/vault step needs it.

## Accuracy + speed + barge-in (round 5)

Symptoms: rambling answers about Nexus's OWN `server.py`, 30-120s latency, no
interrupt while audio plays. Fixes:

- **Concise human persona** (`SYSTEM_PROMPT`) — answer like a colleague, use the
  `[ACTIVE SCREEN]` block for "open file" questions, never dump code, say "I
  can't see a file open" instead of guessing.
- **Short voice answers** — `VOICE_CLAUSE` forces 1-2 spoken sentences; token cap
  `num_predict` is 90 for voice / 256 for text (was 220 for everything). This is
  the main latency fix: short replies generate fast on CPU and make tiny TTS
  (the old replies were ~3.6 MB of audio each).
- **Stricter RAG** (`RAG_MAX_DISTANCE` 0.55 → 0.42) so casual questions don't
  drag in unrelated code chunks.
- **Open-file correlation** — the active file's contents are injected ONLY if its
  name appears in the focused window title; otherwise just the window is shown.
  This stops a stale/scanned file (e.g. `server.py`) from masquerading as "your
  open file" when you're actually in a browser. Scan events (no window) embed
  into the vault but never set the "open file".
- **Barge-in** — the Interrupt button now hard-stops the playing reply
  (`stopSpeech()`) as well as telling the server to stop.

> ⚠️ Point `VITE_NEXUS_WORKSPACE` at YOUR project, not the Nexus AI repo —
> otherwise the vault fills with Nexus's own code and retrieval echoes it back.

## Reply not shown/heard + orb wake (round 6)

The server produced a correct answer ("models.py … in VS Code") but the client
showed/played nothing. Two delivery bugs + an orb request:

- **No audio.** The Web Audio context was created during a socket message, not a
  user gesture, so WebKitGTK left it `suspended` → silent. Fix: `useTtsPlayback`
  now exposes `prime()` (called on the toggle click) AND unlocks on the first
  pointer/key event in the window. Audio now plays.
- **Voice Q&A didn't appear in the chat.** `ChatPanel` only rendered typed
  messages. Now it renders voice turns too (from the server's `{type:"user"}`
  echo + streamed answer), while a `typedEchoPendingRef` flag prevents
  double-rendering your own typed messages. `answer` sets the authoritative full
  text.
- **Orb menu.** Right-click now shows **Wake Up** (☀️ → activate) when the agent
  is sleeping/off, and **Sleep** (🌙) when it's active.

## Stale file + speed + listening (round 7) — matching run_nexus.py

- **THE main bug: stale file content.** The native eyes only read a file on
  SAVE, so switching to another open file left old content → "first 3 lines"
  answered the OLD file. Fixed by porting the local `file_resolver` to Rust:
  every 1s the window thread parses the filename from the window title
  (`extract_filename`), finds it in the workspace (cached, `find_file_in_workspace`),
  and **reads it fresh from disk** (`resolve_active_file`). The "currently open
  file" is now always current, even without saving — exactly like run_nexus.py.
  Non-file windows (browser/terminal) clear the file so nothing stale lingers.
- **Client dedup**: since the eyes now emit every second, `useOsContextSync` only
  forwards an `os_context` when the window/file/length signature changes (no spam,
  no constant re-embedding).
- **Speed (was 30-120s)**:
  - active-file context trimmed 6000 → **2800 chars** (`NEXUS_ACTIVE_FILE_MAX_CHARS`),
  - **RAG skipped entirely when the open file is inlined** — avoids the Ollama
    embed↔chat model swap (a major CPU cost) on "this file" questions,
  - plus the round-5 voice token cap (90). These target prompt-eval + model-swap,
    the real latency drivers on CPU.
- **Listening misses**: VAD threshold lowered 600 → **380** (matches the local
  `VAD_START_RMS`) and min-speech 3 → 2 chunks, so normal clear speech triggers.
- **Mid-conversation disconnects**: usually ngrok free-tier instability. For
  reliable same-machine use, switch the client `.env` to the local URL
  `ws://127.0.0.1:8000/ws` (no tunnel). The client already auto-reconnects.

## Who is "the user"? (invite-key identity)

There is no login screen yet. Identity is decided **at build time** by the
client's `frontend/.env`: `VITE_NEXUS_INVITE_KEY` is sent in the auth handshake,
and the server maps it via `VALID_USERS` to a username (e.g. `nexus_key_44bB` →
`friend_a`). So **every build is hard-wired to one user**. To support multiple
real users you need a runtime **login UI** (enter the key → store in
localStorage → connect) — not yet built; this is the natural next step before
Phase 7.4's Postgres user table.

## Still remaining (next phases)
- **Login UI** to enter the invite key at runtime (multi-user identity).
- Wire the Phase 6.1 native eyes (`nexus://os-context`) to call `syncFile` on
  save so the server's per-tenant memory auto-populates.
- Phase 7.4: replace hardcoded `VALID_USERS` with the Postgres user table.
- Barge-in (interrupt the spoken reply by speaking) — currently the mic is
  paused during playback, so you wait for the reply to finish.
