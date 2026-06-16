import { useCallback, useEffect, useRef, useState } from "react";

/**
 * useTtsPlayback — Phase 7.3 voice-OUT.
 *
 * Plays the server's synthesized speech. The server sends
 * `{ type: "tts_audio", sample_rate, pcm_b64 }` where pcm_b64 is base64-encoded
 * mono Int16 PCM (Piper, 22050 Hz). We decode it to a Web Audio AudioBuffer and
 * play it — clips are scheduled back-to-back so multiple sentences don't overlap.
 *
 * Web Audio playback works reliably in the Tauri/WebKitGTK webview (unlike the
 * Web Speech API, which is unsupported there), so this is the portable path.
 *
 * Returns `playPcm(pcm_b64, sampleRate)`.
 */
export function useTtsPlayback() {
  const ctxRef = useRef(null);
  // Wall-clock (AudioContext time) when the last queued clip finishes, so the
  // next clip starts right after it instead of on top of it.
  const nextStartRef = useRef(0);
  // Live source nodes, so we can hard-stop playback when the agent is turned off.
  const sourcesRef = useRef(new Set());
  // Exposed so the mic can be muted WHILE speech plays (prevents the agent from
  // hearing — and re-asking — its own reply).
  const [isPlaying, setIsPlaying] = useState(false);

  const getCtx = () => {
    if (!ctxRef.current) {
      const AC = window.AudioContext || window.webkitAudioContext;
      ctxRef.current = new AC();
    }
    return ctxRef.current;
  };

  // Create + resume the AudioContext. MUST be called from a user gesture (a
  // click) at least once, or WebKitGTK/Chromium keeps it "suspended" and the
  // first reply plays NOTHING (audio arrives during a socket message, not a
  // gesture). Safe to call repeatedly.
  const prime = useCallback(() => {
    try {
      const ctx = getCtx();
      if (ctx.state === "suspended") ctx.resume();
    } catch {
      /* ignore */
    }
  }, []);

  // Belt-and-braces: also unlock on the very first user interaction anywhere in
  // this window, so audio works even if the agent was armed from the orb window.
  useEffect(() => {
    const unlock = () => prime();
    window.addEventListener("pointerdown", unlock, { once: true });
    window.addEventListener("keydown", unlock, { once: true });
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
    };
  }, [prime]);

  // Stop everything currently playing/queued (e.g. user turned the agent off).
  const stop = useCallback(() => {
    sourcesRef.current.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    });
    sourcesRef.current.clear();
    nextStartRef.current = 0;
    setIsPlaying(false);
  }, []);

  const playPcm = useCallback((pcmB64, sampleRate) => {
    if (!pcmB64) return;
    try {
      const ctx = getCtx();
      if (ctx.state === "suspended") ctx.resume();

      // base64 → bytes → Int16 → Float32 [-1, 1]
      const bin = atob(pcmB64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const pcm = new Int16Array(bytes.buffer);
      const f32 = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;

      const buffer = ctx.createBuffer(1, f32.length, sampleRate || 22050);
      buffer.getChannelData(0).set(f32);

      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      sourcesRef.current.add(src);
      setIsPlaying(true);
      src.onended = () => {
        sourcesRef.current.delete(src);
        if (sourcesRef.current.size === 0) setIsPlaying(false);
      };

      // Schedule sequentially so back-to-back sentences don't overlap.
      const start = Math.max(ctx.currentTime, nextStartRef.current);
      src.start(start);
      nextStartRef.current = start + buffer.duration;
    } catch (err) {
      console.warn("[tts] playback failed", err);
    }
  }, []);

  useEffect(
    () => () => {
      if (ctxRef.current && ctxRef.current.state !== "closed") {
        ctxRef.current.close().catch(() => {});
      }
    },
    []
  );

  return { playPcm, stop, isPlaying, prime };
}
