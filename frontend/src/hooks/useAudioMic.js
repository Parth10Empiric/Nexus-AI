import { useCallback, useEffect, useRef, useState } from "react";

/**
 * useAudioMic — Phase 6.2 "Client Ears".
 *
 * Captures the microphone in the browser/Tauri webview and emits raw 16kHz mono
 * Int16 PCM chunks, ready to stream over a WebSocket to the Python
 * faster-whisper backend. This replaces Python's `sounddevice` entirely — the
 * client now owns audio capture.
 *
 * Pipeline:
 *
 *   getUserMedia(audio)
 *        │
 *        ▼
 *   AudioContext({ sampleRate: 16000 })   ← native C++ 48k→16k downsample
 *        │
 *   MediaStreamAudioSourceNode
 *        │
 *   AudioWorkletNode("pcm-worker")        ← Float32 → Int16, 4096-sample chunks
 *        │ port.onmessage (ArrayBuffer, transferred)
 *        ▼
 *   onAudioChunk(Int16Array)              ← parent plugs this into a WebSocket
 *
 * @param {(chunk: Int16Array) => void} [onAudioChunk]
 *        Called on the main thread for every Int16 PCM chunk. Stored in a ref,
 *        so changing it between renders never tears down the audio graph.
 *
 * @returns {{
 *   startRecording: () => Promise<void>,
 *   stopRecording: () => void,
 *   isRecording: boolean,
 *   error: string | null,
 * }}
 */

// Public-folder asset → served at the site root in dev AND in the Tauri build.
// BASE_URL keeps it correct even if a non-"/" base is configured later.
const WORKLET_URL = `${import.meta.env.BASE_URL}pcm-worker.js`;
const TARGET_SAMPLE_RATE = 16000;

export function useAudioMic(onAudioChunk) {
  const [isRecording, setIsRecording] = useState(false);
  const [error, setError] = useState(null);

  // Latest callback without re-subscribing the worklet port.
  const onChunkRef = useRef(onAudioChunk);
  onChunkRef.current = onAudioChunk;

  // Live audio-graph handles, kept in refs so they survive re-renders and are
  // reachable from cleanup. All null when not recording.
  const streamRef = useRef(null); // MediaStream (mic tracks)
  const contextRef = useRef(null); // AudioContext(16000)
  const sourceRef = useRef(null); // MediaStreamAudioSourceNode
  const workletRef = useRef(null); // AudioWorkletNode("pcm-worker")

  /**
   * Tear the whole graph down and release the microphone. Idempotent: safe to
   * call when already stopped, and safe to call from the unmount cleanup.
   */
  const stopRecording = useCallback(() => {
    // Disconnect nodes first so no more chunks are produced mid-teardown.
    if (workletRef.current) {
      workletRef.current.port.onmessage = null;
      try {
        workletRef.current.disconnect();
      } catch {
        /* already disconnected */
      }
      workletRef.current = null;
    }

    if (sourceRef.current) {
      try {
        sourceRef.current.disconnect();
      } catch {
        /* already disconnected */
      }
      sourceRef.current = null;
    }

    // Stop every mic track → turns off the OS "recording" indicator.
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }

    // Close the AudioContext last (frees the underlying audio thread).
    if (contextRef.current) {
      const ctx = contextRef.current;
      contextRef.current = null;
      if (ctx.state !== "closed") {
        ctx.close().catch(() => {
          /* nothing actionable if close fails */
        });
      }
    }

    setIsRecording(false);
  }, []);

  /**
   * Acquire the mic and build the capture graph. Resolves once chunks are
   * flowing; on any failure it cleans up partial state and surfaces `error`.
   */
  const startRecording = useCallback(async () => {
    setError(null);

    // Guard: never stack two graphs.
    if (contextRef.current) return;

    try {
      // 1. Permission + raw stream. `audio: true` is enough; the AudioContext
      //    sample rate (below) is what forces the 16kHz decimation.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      // 2. 16kHz context → the browser's native engine resamples 48k→16k for
      //    us. No manual JS downsampling math anywhere.
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const context = new AudioCtx({ sampleRate: TARGET_SAMPLE_RATE });
      contextRef.current = context;

      // Some browsers start contexts "suspended" until a user gesture; resume
      // so the worklet actually runs. (startRecording is gesture-triggered.)
      if (context.state === "suspended") {
        await context.resume();
      }

      // 3. Load the off-thread PCM processor module.
      await context.audioWorklet.addModule(WORKLET_URL);

      // If the user hit stop while addModule was awaiting, bail cleanly.
      if (contextRef.current !== context) return;

      // 4. Wire mic → worklet.
      const source = context.createMediaStreamSource(stream);
      sourceRef.current = source;

      const worklet = new AudioWorkletNode(context, "pcm-worker", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1, // force mono
      });
      workletRef.current = worklet;

      // 5. Receive Int16 chunks on the main thread and hand them to the parent.
      worklet.port.onmessage = (event) => {
        const cb = onChunkRef.current;
        if (cb) cb(new Int16Array(event.data));
      };

      // Connecting the worklet to destination keeps the graph "pulled" so
      // process() is invoked. The worklet writes no output, so this is silent —
      // there is no mic echo.
      source.connect(worklet);
      worklet.connect(context.destination);

      setIsRecording(true);
    } catch (err) {
      // Roll back any half-built graph and report a friendly message.
      stopRecording();
      const name = err && err.name ? err.name : "Error";
      const msg =
        name === "NotAllowedError"
          ? "Microphone permission denied."
          : name === "NotFoundError"
          ? "No microphone found."
          : err && err.message
          ? err.message
          : "Failed to start microphone.";
      setError(msg);
    }
  }, [stopRecording]);

  // Safety net: always release the mic if the component unmounts mid-recording.
  useEffect(() => stopRecording, [stopRecording]);

  return { startRecording, stopRecording, isRecording, error };
}
