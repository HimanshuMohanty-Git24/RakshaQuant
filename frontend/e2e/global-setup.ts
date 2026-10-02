// Starts the real web console in the demo environment, reads its one-time launch URL from
// stdout, waits for the demo session to finish, and stops the server afterwards.

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { join } from "node:path";

import { PORT } from "../playwright.config";

const ROOT = join(import.meta.dirname, "..", "..");

function stop(server: ChildProcess): void {
  if (server.pid === undefined || server.exitCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(server.pid), "/T", "/F"], { stdio: "ignore" });
  } else {
    process.kill(-server.pid, "SIGTERM"); // the whole group: uv and its python
  }
}

async function waitFor<T>(what: string, ms: number, probe: () => Promise<T | undefined>): Promise<T> {
  const deadline = Date.now() + ms;
  for (;;) {
    const value = await probe().catch(() => undefined);
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

export default async function globalSetup(): Promise<() => void> {
  const server = spawn(
    "uv",
    ["run", "--extra", "web", "python", "scripts/run_live_trading.py", "--mode", "web", "--demo", "--port", String(PORT)],
    { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: "1" }, detached: process.platform !== "win32" },
  );
  let output = "";
  server.stdout?.on("data", (chunk: Buffer) => (output += chunk.toString()));
  server.stderr?.on("data", (chunk: Buffer) => (output += chunk.toString()));
  try {
    const url = await waitFor("the launch URL", 90_000, async () => {
      if (server.exitCode !== null) throw new Error(`the server exited:\n${output}`);
      return output.match(/http:\/\/\S+#token=[A-Za-z0-9_-]+/)?.[0];
    });
    const token = url.split("#token=")[1]!;
    await waitFor("the demo session to finish", 240_000, async () => {
      const res = await fetch(`http://127.0.0.1:${PORT}/api/summary`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const summary = (await res.json()) as { running: boolean; session?: { state: string } | null };
      return !summary.running && summary.session?.state === "EXIT" ? true : undefined;
    });
    process.env.RQ_URL = url;
    process.env.RQ_TOKEN = token;
  } catch (error) {
    stop(server);
    throw error;
  }
  return () => stop(server);
}
