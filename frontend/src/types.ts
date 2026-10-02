// Mirrors the JSON contract emitted by src/live/recorder.py (snapshot_from_stats) and
// src/live/recorder.py CycleTrace/Span. Keep in sync with the backend.

export type EnvBadge = "PAPER" | "SHADOW" | "LIVE";
export type RunStatus = "IDLE" | "RUNNING" | "DONE" | "ERROR";
export type ActivityLevel = "INFO" | "SUCCESS" | "WARNING" | "ERROR" | "TRADE";

export interface RunInfo {
  status: RunStatus;
  cycle: number;
  mode: string;
  dataSource: string;
  executionMode: string;
  env: EnvBadge;
  marketOpen: boolean;
  sessionStartTs: string;
}

export interface Account {
  startingBalance: number;
  currentBalance: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
  pnlPercent: number;
  bestTrade: number;
  worstTrade: number;
}

export interface Trades {
  total: number;
  winners: number;
  losers: number;
  winRate: number;
}

export interface Agents {
  cyclesRun: number;
  signalsGenerated: number;
  signalsValidated: number;
  signalsRejected: number;
  tradesApproved: number;
  tradesRiskRejected: number;
  approvalRate: number;
}

export interface Regime {
  current: string;
  confidence: number;
  strategies: string[];
}

export interface FinOps {
  calls: number;
  tokens: number;
  costUsd: number;
}

export interface Goal {
  enabled: boolean;
  feasible: boolean;
  targetAmount: number;
  mtdPnl: number;
  expectedToDate: number;
  onPace: boolean;
  status: string;
}

export interface Position {
  symbol: string;
  side: string;
  qty: number;
  entry: number;
  pnl: number;
}

export interface Quote {
  symbol: string;
  ltp: number;
  changePercent: number;
}

export interface Decision {
  signalType: string;
  symbol: string;
  strategy: string;
  confidence: number;
  reason: string;
}

export interface ActivityEntry {
  time: string;
  level: ActivityLevel;
  message: string;
}

export interface Snapshot {
  ts: string;
  run: RunInfo;
  account: Account;
  trades: Trades;
  agents: Agents;
  regime: Regime;
  finops: FinOps;
  goal: Goal;
  positions: Position[];
  quotes: Quote[];
  decision: Decision;
  activity: ActivityEntry[];
}

export type SpanKind = "llm" | "deterministic" | "support";

export interface Span {
  id: string;
  name: string;
  label: string;
  kind: SpanKind;
  decision: string;
  confidence: number | null;
  reasoning: string;
  inputTokens: number;
  outputTokens: number;
  tokens: number;
  costUsd: number;
  latencyMs: number | null;
  detail: Record<string, unknown>;
}

export interface CycleTrace {
  id: string;
  workflowId: string;
  ts: string;
  status: string;
  durationMs: number;
  regime: string;
  regimeConfidence: number;
  signalsCount: number;
  approvedCount: number;
  rejectedCount: number;
  tokens: number;
  costUsd: number;
  spans: Span[];
}

// GET /api/config (subset). The v2 engine is paper only.
export interface AppConfig {
  environment: string;
  mode: "paper";
  execution_mode_requested: string;
  execution_mode_note: string | null;
  market_data_source: string;
  read_only: boolean;
}

// The /ws event stream (plan M9.4); this console only uses the legacy `console` slot.
export type WsMessage =
  | { type: "subscribed"; data: { topics: string[]; since_seq: number; replay_to: number } }
  | { type: "console"; data: Snapshot }
  | { type: "summary"; data: { demo: boolean; running: boolean } }
  | { type: "stopped" }
  | { type: "error"; data: { message: string } }
  | { type: "heartbeat" | "resync"; data: Record<string, unknown> };

export type ConnState = "connecting" | "open" | "reconnecting" | "closed";
