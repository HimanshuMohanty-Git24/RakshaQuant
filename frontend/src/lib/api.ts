// Thin REST client for the run-control + config endpoints. Live state arrives over the
// WebSocket (see useLiveState); these are the imperative actions + cold-load helpers.

import type { AppConfig } from "../types";
import { authHeaders } from "./auth";

async function post(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export async function startRun(opts: { demo?: boolean } = {}): Promise<{
  ok: boolean;
  error?: string;
}> {
  const res = await post("/api/session/start", { demo: !!opts.demo });
  if (res.ok) return { ok: true };
  const data = await res.json().catch(() => ({}));
  return { ok: false, error: data.error ?? `HTTP ${res.status}` };
}

export async function stopRun(): Promise<void> {
  await post("/api/session/stop");
}

export async function fetchConfig(): Promise<AppConfig | null> {
  try {
    const res = await fetch("/api/config", { headers: authHeaders() });
    if (!res.ok) return null;
    return (await res.json()) as AppConfig;
  } catch {
    return null;
  }
}
