/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// FastAPI serves the built SPA at "/" (with an index.html fallback for client routes). The dev
// server proxies the API and the event stream to the backend (start it with `--mode web --dev`;
// it allows the Vite origin).
export default defineConfig({
  plugins: [react()],
  base: "/", // absolute: deep links (/decisions/<id>) load the same assets
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
    manifest: true, // scripts/check-bundle.mjs measures the initial route from it
    target: "es2022",
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
