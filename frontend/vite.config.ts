import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" so the built asset URLs are relative and work when FastAPI serves the SPA at "/".
// The dev server proxies the API + WebSocket to the FastAPI backend (run with `--mode web --dev`).
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
