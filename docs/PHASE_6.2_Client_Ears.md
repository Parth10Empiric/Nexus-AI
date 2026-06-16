# Phase 6.2 — Client Ears (browser-native mic capture)

**Goal:** deprecate Python `sounddevice` and move microphone capture into the
React frontend (inside Tauri). The client now records the mic, downsamples to
**16kHz mono**, converts Float32 → **Int16 PCM**, and emits chunks ready to
stream over a WebSocket to the Python `faster-whisper` backend.

This is the audio half of the Phase 6 Client-Server transition (Phase 6.1 did
the "eyes"; this is the "ears").

## What was built

| Piece | File | Role |
|-------|------|------|
| PCM processor | `frontend/public/pcm-worker.js` | Off-thread Float32→Int16, 4096-sample chunks |
| Capture hook | `frontend/src/hooks/useAudioMic.js` | getUserMedia + AudioContext(16000) lifecycle |

## Pipeline

```
getUserMedia({ audio: true })
      │
      ▼
AudioContext({ sampleRate: 16000 })      ← native C++ 48k→16k decimation
      │
MediaStreamAudioSourceNode
      │
AudioWorkletNode("pcm-worker")           ← runs on the audio render thread
      │  Float32 [-1,1] → Int16 [-32768,32767], accumulate 4096 samples
      │  port.postMessage(buffer, [buffer])   (zero-copy transfer)
      ▼
worklet.port.onmessage  →  onAudioChunk(Int16Array)   → (next: WebSocket)
```

## Key design decisions

- **No manual JS resampling.** We construct `AudioContext({ sampleRate: 16000 })`
  and let the browser's native engine handle 48k→16k. The worklet only does the
  Float32→Int16 type conversion.
- **Off the main thread.** Conversion runs in an `AudioWorkletProcessor`, so
  React never janks. Chunks are *transferred* (not copied) to the main thread.
- **Chunk size 4096** = 256ms at 16kHz — a good latency/overhead point for
  streaming STT.
- **Mono Int16** is exactly what faster-whisper expects, so the backend can feed
  the bytes straight in with no conversion.
- **Clean teardown.** `stopRecording` disconnects nodes, stops mic tracks (kills
  the OS recording indicator), and closes the AudioContext. The hook also tears
  down on unmount. `startRecording` rolls back partial state on any error and
  reports friendly messages (permission denied / no mic).

## Usage

```jsx
import { useAudioMic } from "./hooks/useAudioMic.js";

function VoiceButton({ socket }) {
  const { startRecording, stopRecording, isRecording, error } = useAudioMic(
    (chunk) => socket?.send(chunk.buffer) // Int16Array → WebSocket
  );
  return (
    <button onClick={isRecording ? stopRecording : startRecording}>
      {error ?? (isRecording ? "Stop" : "Speak")}
    </button>
  );
}
```

## How to test

Capture can be tested **standalone**, before any WebSocket exists — just log the
chunks and inspect their shape.

### 1. Add a temporary test button
Drop this into a mounted component (e.g. `src/App.jsx`); remove it after testing:
```jsx
import { useAudioMic } from "./hooks/useAudioMic.js";
// inside the component:
const seen = useRef({ chunks: 0, samples: 0 });
const { startRecording, stopRecording, isRecording, error } = useAudioMic((chunk) => {
  seen.current.chunks += 1;
  seen.current.samples += chunk.length;
  console.log("[mic chunk]", chunk.length, "samples — total", seen.current);
});
// ...in JSX:
<button onClick={isRecording ? stopRecording : startRecording}>
  {error ?? (isRecording ? "Stop mic" : "Start mic")}
</button>
```

### 2. Run it
```bash
cd frontend && npm run tauri:dev       # or: npm run dev  (plain browser)
```
> The webview/browser must allow mic access. In a plain browser, `localhost` is a
> secure context so `getUserMedia` works. In the Tauri build, the OS prompts for
> mic permission the first time.

### 3. Verify the chunk stream
- Click **Start mic** and grant permission.
- The console logs `[mic chunk] 4096 samples` repeatedly — roughly **~4 chunks
  per second** (4096 samples ÷ 16000 Hz ≈ 256ms each). Speaking does not change
  the rate (PCM is continuous), only the values.

### 4. Verify the format (paste in DevTools while recording)
```js
// Confirm the native engine really gave us a 16kHz context:
new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }).sampleRate
// → 16000
```
Inspect a logged chunk: it's an **`Int16Array`**, `length === 4096`, values in
`[-32768, 32767]`. Silence ≈ values near 0; speaking pushes them toward the rails.

### 5. (Optional) Prove it's real audio — dump a WAV
Temporarily collect a few seconds of chunks, then in DevTools build a 16kHz mono
WAV and play it back to confirm the PCM is intelligible:
```js
// concat your captured Int16Array chunks into one `pcm` Int16Array first, then:
function wav(pcm, rate = 16000) {
  const b = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(b);
  const s = (o, str) => [...str].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  s(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); s(8, "WAVE");
  s(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  s(36, "data"); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(b, 44).set(pcm);
  return new Blob([b], { type: "audio/wav" });
}
new Audio(URL.createObjectURL(wav(pcm))).play();
```

### 6. Verify clean teardown
- Click **Stop mic**: the OS/browser "recording" indicator turns off, and chunk
  logs stop immediately.
- Navigate away / unmount the component while recording → mic still releases
  (the hook's unmount cleanup runs).
- **Permission test:** deny the mic prompt → `error` shows
  "Microphone permission denied." and no graph is left half-built.

## Notes / next

- The worklet is served from `public/` → available at `/pcm-worker.js` in both
  `vite dev` and the Tauri build (`addModule` uses `import.meta.env.BASE_URL`).
- **Next (Phase 6.x):** wire `onAudioChunk` into the WebSocket transport to the
  faster-whisper backend and stream partial transcripts back. Capture is done;
  transport is the remaining piece.
