/**
 * pcm-worker.js — Phase 6.2 "Client Ears" AudioWorkletProcessor.
 *
 * Runs on the dedicated audio render thread (NOT the main/UI thread), so the
 * conversion never janks React. The browser's native C++ engine has already
 * downsampled the mic to 16kHz mono before frames arrive here (we ask for a
 * 16kHz AudioContext in useAudioMic.js), so this file does ZERO resampling.
 *
 * Per render quantum the engine hands us 128 Float32 samples in [-1.0, 1.0].
 * We:
 *   1. Convert each Float32 sample to signed 16-bit PCM [-32768, 32767].
 *   2. Accumulate into a fixed CHUNK_SIZE buffer.
 *   3. Once full, postMessage the Int16Array's ArrayBuffer to the main thread,
 *      transferring (zero-copy) ownership so it's cheap to ship onward to a
 *      WebSocket → faster-whisper.
 *
 * Registered as "pcm-worker" via registerProcessor at the bottom.
 */

// faster-whisper streams happily on ~256ms windows. At 16kHz, 4096 samples is
// 256ms — a good latency/overhead balance. Must be a positive integer.
const CHUNK_SIZE = 4096;

class PCMWorker extends AudioWorkletProcessor {
  constructor() {
    super();
    // Pre-allocated accumulation buffer + write cursor. Reused across quanta so
    // we don't allocate on the audio thread (allocations there cause glitches).
    this._buffer = new Int16Array(CHUNK_SIZE);
    this._offset = 0;
  }

  /**
   * Called by the engine for every 128-sample render quantum.
   * @param {Float32Array[][]} inputs - inputs[0] = first input's channels.
   * @returns {boolean} true to keep the processor alive.
   */
  process(inputs) {
    // We requested a mono source, so we only read channel 0 of input 0.
    const channel = inputs[0] && inputs[0][0];

    // No input this quantum (mic not yet flowing / node disconnected): keep
    // alive but do nothing.
    if (!channel || channel.length === 0) {
      return true;
    }

    for (let i = 0; i < channel.length; i++) {
      // Clamp guards against rare out-of-range values from upstream nodes.
      let s = channel[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;

      // Asymmetric scale: negative range is 32768, positive is 32767. This is
      // the standard, distortion-free Float32 → Int16 mapping.
      this._buffer[this._offset++] = s < 0 ? s * 0x8000 : s * 0x7fff;

      // Chunk full → ship it and reset the cursor.
      if (this._offset === CHUNK_SIZE) {
        // Copy out so the next quantum can keep writing into a fresh buffer
        // while this one is transferred to the main thread.
        const out = this._buffer.slice(0); // -> new Int16Array
        this.port.postMessage(out.buffer, [out.buffer]); // transfer (zero-copy)
        this._offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("pcm-worker", PCMWorker);
