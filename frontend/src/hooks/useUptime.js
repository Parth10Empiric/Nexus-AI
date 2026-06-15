import { useEffect, useRef, useState } from "react";

/**
 * useUptime — a non-blocking session clock.
 *
 * Returns an "MM:SS" string that ticks once per second. It uses a ref to hold
 * the immutable session start time, so re-renders never reset it, and a single
 * interval that only calls setState with a tiny string — no heavy work on the
 * timer, so it can never block the render thread or cause UI lag.
 */
export function useUptime() {
  const startRef = useRef(Date.now());
  const [label, setLabel] = useState("00:00");

  useEffect(() => {
    const id = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startRef.current) / 1000);
      const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
      const ss = String(elapsed % 60).padStart(2, "0");
      setLabel(`${mm}:${ss}`);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  return label;
}
