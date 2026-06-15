/** @type {import('tailwindcss').Config} */
// Phase 1.2 — Enterprise Dark Mode design tokens.
// Every color/spacing/animation the UI uses is defined here so the look is
// consistent and tunable from one place.
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  darkMode: "class", // we force dark via a class on <html>; never auto-light.
  theme: {
    extend: {
      colors: {
        // Structural surfaces — the "deep grays / charcoal" the spec asks for.
        nexus: {
          bg: "#121212", // deepest structural background
          surface: "#1e1e1e", // charcoal card surface
          elevated: "#262626", // slightly raised surface (hover / nested)
          border: "#2e2e2e", // hairline borders
          borderStrong: "#3a3a3a",
          // Text ramp — bright headline → muted metadata.
          text: "#ececec",
          muted: "#9a9a9a",
          faint: "#6b6b6b",
          // Bright accent highlights.
          accent: "#4f9dff", // primary accent (links, focus, active app)
          online: "#3ddc84", // tracker connected
          offline: "#ff5c5c", // tracker disconnected
        },
      },
      fontFamily: {
        // System UI stack first (crisp, no web-font flash), mono for titles.
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        card: "0 1px 2px rgba(0,0,0,0.4), 0 2px 8px rgba(0,0,0,0.25)",
      },
      keyframes: {
        // Soft pulse for the live status dot.
        pulse: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
        // Gentle fade for new content (no movement → zero layout shift).
        fadein: {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        // ---- UI Phase 1 · Floating Agent Orb states -------------------------
        // Slow "alive" breathing for Standby/Sleeping.
        breathe: {
          "0%, 100%": { transform: "scale(0.94)", opacity: "0.65" },
          "50%": { transform: "scale(1.04)", opacity: "1" },
        },
        // Expanding halo ring for the Listening state.
        halo: {
          "0%": { transform: "scale(0.85)", opacity: "0.55" },
          "70%": { transform: "scale(1.7)", opacity: "0" },
          "100%": { transform: "scale(1.7)", opacity: "0" },
        },
        // Rotating conic ring for the Thinking state.
        orbit: {
          from: { transform: "rotate(0deg)" },
          to: { transform: "rotate(360deg)" },
        },
        // Per-bar bounce for the Speaking waveform.
        wave: {
          "0%, 100%": { transform: "scaleY(0.35)" },
          "50%": { transform: "scaleY(1)" },
        },
      },
      animation: {
        "pulse-slow": "pulse 2s cubic-bezier(0.4,0,0.6,1) infinite",
        fadein: "fadein 240ms ease-out",
        breathe: "breathe 3.6s cubic-bezier(0.4,0,0.6,1) infinite",
        halo: "halo 1.8s cubic-bezier(0.4,0,0.6,1) infinite",
        orbit: "orbit 2.4s linear infinite",
        wave: "wave 0.9s ease-in-out infinite",
      },
      transitionTimingFunction: {
        nexus: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
    },
  },
  plugins: [],
};
