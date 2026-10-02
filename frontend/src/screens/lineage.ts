// Typed views over a decision's recorded events (GET /api/decisions/{id}). Payloads arrive as
// JSON objects; these interfaces name only the fields the inspector reads, all defensively.

import type { Schemas } from "../api/client";

export type LineageEvent = Schemas["LineageEvent"];

export interface Reason {
  name: string;
  value: number | string | null;
  detail?: string;
}

export interface SignalData {
  signal_id: string;
  strategy: string;
  side: string;
  instrument_key: string;
  agreement_score: number;
  bar_date: string;
  generated_at: string;
  is_shadow: boolean;
  stop_atr_mult?: number;
  target_atr_mult?: number;
  reasons: Reason[];
}

export interface CheckData {
  code: string;
  level: string;
  outcome: string;
  observed: number | string | null;
  limit: number | string | null;
  message: string;
  max_qty: number | null;
}

export interface RiskData {
  outcome: string;
  kind: string; // open, close, reduce, flatten
  qty_requested: number | null;
  qty_approved: number;
  notional: string | null;
  risk_amount: string | null;
  risk_pct_equity: number | null;
  ref_price: string | null;
  stop_price: string | null;
  target_price: string | null;
  limits_hash: string;
  signal_id: string | null;
  reasons: CheckData[];
  snapshot?: { quote_age_s?: number | null; data_source?: string | null; equity?: string };
}

export interface VerdictData {
  advisor: string;
  verdict: string;
  confidence: number | null;
  abstain_reason: string | null;
  model: string | null;
  provider: string | null;
  latency_ms: number | null;
  reasons: { claim: string; evidence_ref: string }[];
}

export interface DispositionData {
  signal_id: string;
  strategy: string;
  disposition: string;
  detail: string;
}

export interface TradeData {
  trade_id: string;
  exit_reason: string;
  entry_price: string;
  exit_price: string;
  quantity: number;
  gross_pnl: string;
  charges: string;
  net_pnl: string;
  entry_ts: string;
  exit_ts: string;
  exit_decision_id: string | null;
}

export interface ShadowData {
  signal_id: string;
  strategy: string;
  entry_price: string;
  exit_price?: string;
  exit_reason?: string;
  net_pnl?: string;
  net_return_pct?: number;
  stop_price?: string;
  target_price?: string;
  is_shadow_strategy: boolean;
}

export interface AlphaData {
  signal_id: string;
  net_return_pct: number;
  nifty_return_pct: number;
  alpha_pct: number;
}

export interface FillData {
  fill: { client_order_id: string; price: string; quantity: number; charges: string; ts: string; charges_breakdown?: Record<string, string> };
}

export interface ModelCallData {
  task: string;
  model: string;
  checkpoint: string;
  latency_ms: number;
  escalated: boolean;
  calibrated: boolean;
  answers: Record<string, { value?: unknown; probabilities?: Record<string, number>; confidence?: number }>;
}

export interface LLMCallData {
  role: string;
  provider: string;
  model: string;
  outcome: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  cost_inr: string | null;
}

export const data = <T>(e: LineageEvent): T => e.data as unknown as T;

/** The events of one type, optionally for one book. */
export function ofType(events: LineageEvent[], type: string, book?: string | null): LineageEvent[] {
  return events.filter((e) => e.type === type && (book === undefined || e.book_id === book));
}

/** Books that appear in the lineage, in order. */
export function booksOf(events: LineageEvent[]): string[] {
  return [...new Set(events.map((e) => e.book_id).filter((b): b is string => !!b))].sort();
}
