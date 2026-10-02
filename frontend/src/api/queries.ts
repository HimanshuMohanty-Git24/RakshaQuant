// TanStack Query hooks over the REST projections (§6.6). Stored events arriving on the stream
// invalidate the projections they change (at most once a second), so screens stay current
// without polling; the summary itself is the stream's latest-wins slot.

import { QueryClient, useQuery, type QueryKey } from "@tanstack/react-query";

import { useStream } from "../lib/stream";
import { ApiError, get, type Query } from "./client";

export const keys = {
  summary: ["summary"] as const,
  system: ["system"] as const,
  config: ["config"] as const,
  books: ["books"] as const,
  positions: (book?: string) => ["positions", book ?? "all"] as const,
  orders: (q: Query) => ["orders", q] as const,
  fills: (q: Query) => ["fills", q] as const,
  trades: (q: Query) => ["trades", q] as const,
  decisions: (q: Query) => ["decisions", q] as const,
  decision: (id: string) => ["decision", id] as const,
  risk: (book?: string) => ["risk", book ?? "all"] as const,
  aiCalls: (q: Query) => ["ai", "calls", q] as const,
  aiSpend: (q: Query) => ["ai", "spend", q] as const,
  aiModels: ["ai", "models"] as const,
  decisionModels: (q: Query) => ["ai", "decision-models", q] as const,
  bars: (symbol: string, days: number) => ["bars", symbol, days] as const,
  typed: (q: Query) => ["typed", q] as const,
  report: (day: string) => ["report", day] as const,
};

/** Which projections each stream topic can change. */
export const INVALIDATES: Record<string, string[]> = {
  orders: ["orders", "fills", "positions", "summary", "decision"],
  positions: ["positions", "trades", "books", "summary", "decision"],
  decisions: ["decisions", "decision"],
  risk: ["risk", "summary"],
  ai: ["ai", "decision"],
  market: ["typed", "bars"],
  system: ["system", "alerts"],
};

/** Collects the topics of applied events and invalidates their queries at most once a second. */
export function invalidator(client: QueryClient, intervalMs = 1_000) {
  const due = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  const flush = () => {
    timer = null;
    for (const prefix of due) void client.invalidateQueries({ queryKey: [prefix] });
    due.clear();
  };
  return (topics: Set<string>, resync: boolean) => {
    if (resync) {
      void client.invalidateQueries(); // the projections are the truth: refetch them all
      return;
    }
    for (const topic of topics) for (const prefix of INVALIDATES[topic] ?? []) due.add(prefix);
    if (due.size && timer === null) timer = setTimeout(flush, intervalMs);
  };
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

// -- hooks used by the shell (screens use useQuery with `keys` directly) --------------------------

export function useSummary() {
  const live = useStream((s) => s.summary);
  const query = useQuery({
    queryKey: keys.summary,
    queryFn: ({ signal }) => get("/api/summary", { signal }),
    refetchInterval: 10_000, // a fallback: the stream's slot is fresher whenever it is open
  });
  return { data: live ?? query.data ?? null, error: query.error };
}

export function useSystem() {
  return useQuery({
    queryKey: keys.system,
    queryFn: ({ signal }) => get("/api/system", { signal }),
    refetchInterval: 5_000,
  });
}

export function useConfig() {
  return useQuery({
    queryKey: keys.config,
    queryFn: ({ signal }) => get("/api/config", { signal }),
    staleTime: 60_000,
  });
}

export type Key = QueryKey;
