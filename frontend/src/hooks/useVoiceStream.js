import { useCallback, useEffect, useRef } from "react";
import { useAudioMic } from "./useAudioMic.js";

/**
 * useVoiceStream — Phase 7.3 voice-in pipeline.
 *
 * When the agent is listening, this captures the mic (Phase 6.2), runs a simple
 * energy-based VAD to find utterance boundaries, and streams the Int16 PCM to
 * the server over the WebSocket. On end-of-utterance it calls `endAudio()` so
 * the server transcribes (faster-whisper) and answers.
 *
 * There is NO wake word in the browser client (openwakeword was a local Python
 * engine). Instead, arming the Voice Agent toggle = "start listening". While
 * listening you just speak; ~1s of silence ends the utterance automatically.
 *
 * @param {boolean} active  true while agentState === "listening".
 * @param {{ sendAudioChunk:(i:Int16Array)=>void, endAudio:()=>void }} socket
 */

// RMS energy (Int16 scale) above which a chunk counts as speech, not silence.
// Matches the local app's VAD_START_RMS (400) so normal speech reliably
// triggers — 600 was high enough to miss softer/clear speech.
const RMS_THRESHOLD = 380;
// Trailing low-energy chunks that end an utterance. Chunk = 4096 samples @16kHz
// ≈ 256ms, so 4 ≈ ~1s of silence.
const SILENCE_CHUNKS = 4;
// Require at least this much real speech before we'll end an utterance, so a
// single click/noise chunk can't fire a (hallucinated) question.
const MIN_SPEECH_CHUNKS = 2;
// Hard cap per utterance (~20s) so a noisy room can't stream forever.
const MAX_CHUNKS = 80;

export function useVoiceStream(active, { sendAudioChunk, endAudio }) {
  // VAD state across chunks (refs → no re-renders on the audio hot path).
  const speaking = useRef(false);
  const silence = useRef(0);
  const sent = useRef(0);
  // Latch: once we've sent end-of-utterance we STOP capturing until the server
  // cycles back to "listening" (active flips false→true). Without this, the mic
  // keeps running in the gap before the server replies and grabs stray noise →
  // a second bogus question and out-of-order/old replies.
  const awaitingReply = useRef(false);

  // Keep latest socket fns without re-subscribing the mic.
  const sendRef = useRef(sendAudioChunk);
  const endRef = useRef(endAudio);
  sendRef.current = sendAudioChunk;
  endRef.current = endAudio;

  const resetUtterance = () => {
    speaking.current = false;
    silence.current = 0;
    sent.current = 0;
  };

  const onChunk = useCallback((int16) => {
    // After an utterance ends, ignore everything until the next listening turn.
    if (awaitingReply.current) return;

    // Mean-square energy of the chunk.
    let sum = 0;
    for (let i = 0; i < int16.length; i++) sum += int16[i] * int16[i];
    const rms = Math.sqrt(sum / int16.length);

    if (rms > RMS_THRESHOLD) {
      speaking.current = true;
      silence.current = 0;
    } else if (speaking.current) {
      silence.current += 1;
    }

    // Stream only once speech has started — skips leading silence so the
    // server's buffer (and whisper's job) stays small.
    if (speaking.current) {
      sendRef.current(int16);
      sent.current += 1;
    }

    const endedBySilence =
      speaking.current && sent.current >= MIN_SPEECH_CHUNKS && silence.current >= SILENCE_CHUNKS;
    const endedByLength = sent.current >= MAX_CHUNKS;
    if (endedBySilence || endedByLength) {
      endRef.current(); // server: utterance complete → transcribe + answer
      awaitingReply.current = true; // stop until the next listening turn
      resetUtterance();
    }
  }, []);

  const { startRecording, stopRecording, error } = useAudioMic(onChunk);

  useEffect(() => {
    if (active) {
      // Fresh listening turn → clear the latch and capture again.
      awaitingReply.current = false;
      resetUtterance();
      startRecording();
    } else {
      stopRecording();
      resetUtterance();
    }
    return () => stopRecording();
  }, [active, startRecording, stopRecording]);

  return { micError: error };
}
