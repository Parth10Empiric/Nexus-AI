import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri expects a fixed port and serves the built assets from dist/.
// These settings make `vite dev` cooperate with `tauri dev`.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: false,
    // --- CORS bypass for local Ollama (dev only) ---------------------------
    // The browser blocks cross-origin fetches from localhost:1420 to
    // localhost:11434. By proxying "/ollama/*" through Vite, the frontend
    // makes a SAME-ORIGIN request and Vite forwards it server-side, where CORS
    // does not apply. ollama.js targets "/ollama" whenever import.meta.env.DEV.
    proxy: {
      "/ollama": {
        target: "http://localhost:11434",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/ollama/, ""),
      },
    },
  },
  // Produce assets Tauri can bundle; modern target since the webview is current.
  build: {
    target: "es2021",
    minify: "esbuild",
    sourcemap: false,
  },
});
