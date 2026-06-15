# Nexus AI — Phase 2.1: The AI Brain (Local LLM via Ollama)

> A learning guide. Plain-language explanation of **what** we built, **why**
> each tool, and **how** the streaming + context-injection works under the hood.

---

## 1. What is this phase, in one sentence?

We gave the dashboard a **brain**: a chat panel that talks to a local Ollama
model (`qwen2.5-coder:1.5b`), streams its answer out token-by-token, and
silently tells the model what window you're in so its advice is context-aware —
all without a single byte leaving your machine.

---

## 2. The big picture

```
  Phase 1 useActiveWindow() ──▶ App.jsx ──▶ aiContext {appName, title}
                                                │
                                                ▼
   ChatPanel ── user question + aiContext ──▶ streamGenerate()  (lib/ollama.js)
                                                │  POST /api/generate (stream)
                                   dev: /ollama proxy │  prod: http://localhost:11434
                                                ▼
                                   Ollama (qwen2.5-coder:1.5b)
                                                │  NDJSON: {response,done} per line
                                                ▼
                          tokens append to the last bubble → types out live
```

---

## 3. Tools & why

| Tool | Why |
|---|---|
| **Ollama** | Runs open LLMs locally on CPU. Private, free, no cloud API. Exposes a simple HTTP API on `:11434`. |
| **`qwen2.5-coder:1.5b`** | Small (~1 GB, Q4) coding model that runs fast on the i5-6500 with no GPU. |
| **Browser `fetch` + `ReadableStream`** | Reads the response **as it arrives** instead of waiting for the whole thing — the key to live typing without UI freeze. |
| **Vite dev proxy** | Bypasses CORS in development by making the request same-origin. |
| **Tauri CSP `connect-src`** | Allows the production webview to reach `localhost:11434`. |

---

## 4. How the streaming works (under the hood)

Ollama's `/api/generate` returns **NDJSON** — one JSON object per line:

```
{"response":"This","done":false}
{"response":" file","done":false}
...
{"response":"","done":true, ...stats...}
```

`streamGenerate()` in [src/lib/ollama.js](../frontend/src/lib/ollama.js) reads it like this:

1. `fetch(...)` resolves as soon as the **headers** arrive — the body is still streaming.
2. `res.body.getReader()` gives a reader that yields **raw byte chunks** as the network delivers them.
3. Each chunk is decoded to text and added to a `buffer`.
4. We split the buffer on `\n`. Every **complete** line is `JSON.parse`d; a trailing **partial** line stays in the buffer until the next chunk completes it.
5. For each parsed object we append `.response` to a running `full` string and fire `onToken(token, full)`. When `done:true` arrives, we return.

Because this is all `await`-driven and only calls `setState` with small strings, **the render thread is never blocked** — React keeps painting each new token smoothly while the model is still thinking.

---

## 5. Context injection — and why it beats copy-paste

`buildSystemPrompt(context)` folds the active window title (from Phase 1) into
the system prompt before sending:

```
...The developer is currently working in: code
Active window/file: "tracker.py — Nexus AI — Visual Studio Code"...
```

Verified live: asked "what language is this file in?" with `Active file: tracker.py`
injected, the model answered **"Python"** — using context the user never typed.

**Why invisible injection wins:**
- **Zero friction** — no selecting, copying, or pasting; the assistant just *knows*.
- **Always fresh** — the context is whatever you're looking at *right now*, not a stale paste.
- **Fewer mistakes** — you can't forget to include the relevant file or paste the wrong one.
- **It feels like "over the shoulder"** — exactly the product promise.

---

## 6. CORS / config — what changed

- **`vite.config.js`** — added a `server.proxy` for `/ollama` → `http://localhost:11434`. In dev the frontend calls `/ollama/...` (same-origin), Vite forwards it server-side, so the browser CORS check never fires.
- **`tauri.conf.json`** — `connect-src 'self' http://localhost:11434 ipc: http://ipc.localhost` so the production webview is allowed to reach Ollama.
- **`ollama.js`** picks the base URL automatically: `/ollama` when `import.meta.env.DEV`, the direct URL in a production build.

**Production note:** in a built Tauri app the webview origin is `tauri://localhost`. If Ollama rejects it, start Ollama with `OLLAMA_ORIGINS="*"` (or your specific origin) so it accepts the cross-origin request.

---

## 7. Files in this phase

```
frontend/src/
├── lib/ollama.js              # NEW — streaming client + context prompt builder
├── components/ChatPanel.jsx   # NEW — chat UI (history, composer, live typing)
└── App.jsx                    # UPDATED — 2-col layout, passes live context to chat
frontend/vite.config.js         # UPDATED — /ollama dev proxy (CORS bypass)
frontend/src-tauri/tauri.conf.json  # UPDATED — connect-src CSP
```

---

## 8. How to run

```bash
# 1. Ollama must be running with the model pulled (already done):
ollama serve              # (usually auto-runs as a service)
ollama list               # should show qwen2.5-coder:1.5b

# 2. The app:
cd frontend
npm run dev               # browser at http://localhost:1420 (uses the proxy)
#   or: npm run tauri:dev # native window

# Ask: "Explain this file" — the AI already knows your active window.
```

---

## 9. What's next (Phase 2.2 / 2.3)

- **Hotkey capture** (Phase 2.2): a global shortcut that grabs the active file +
  last few log entries and asks the AI without leaving your editor.
- **Vector memory** (Phase 2.3): embed logs with `nomic-embed-text` into ChromaDB
  so you can ask "what was I working on yesterday?".
