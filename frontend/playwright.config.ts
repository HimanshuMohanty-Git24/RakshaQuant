import { defineConfig } from "@playwright/test";

// End-to-end tests against the real server replaying the demo tape (plan M10.5). The global
// setup starts `run_live_trading.py --mode web --demo` and hands the tests its one-time URL.
// Locally, PW_CHANNEL=msedge drives the installed Edge (no browser download needed).
export const PORT = Number(process.env.RQ_E2E_PORT ?? 8790);

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.PW_CHANNEL || undefined,
    viewport: { width: 1600, height: 960 },
    colorScheme: "dark",
    timezoneId: "America/New_York", // the UI must show IST whatever the browser's zone
    locale: "en-US",
  },
});
