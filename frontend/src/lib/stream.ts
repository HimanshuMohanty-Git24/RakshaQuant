// The live event stream (plan M9.4 server, §6.6 client): one WebSocket, a seq-ordered reducer
// into a zustand store, updates batched per animation frame, resume with `since_seq` after a
// reconnect, and `resync` → refetch the REST projections.

import { create } from "zustand";

import type { Schemas } from "../api/client";
import { wsProtocols } from "./auth";

export type Envelope = Schemas["StreamEnvelope"];
export type Summary = Schemas["Summary"];
export type ConnStatus = "connecting" | "open" | "reconnecting" | "unauthorized";

export interface QuoteView {
  symbol: string;
  ltp: number;
  prev_close: number | null;
  change_pct: number | null;
  exchange_ts: string;
  source: string;
}

const KEPT = 600; // recent events kept for live feeds (alerts, activity)

export interface StreamState {
  status: ConnStatus;
  lastSeq: number; // the highest stored event applied
  events: Envelope[]; // recent stored events, oldest first
  summary: Summary | null; // latest-wins slot
  quotes: Record<string, QuoteView>; // latest-wins slot, by instrument key
  lastMessageAt: number | null;
  notices: Envelope[]; // stopped / error notices (newest last)
}

export const initialStream: StreamState = {
  status: "connecting",
  lastSeq: 0,
  events: [],
  summary: null,
  quotes: {},
  lastMessageAt: null,
  notices: [],
};

export interface Applied {
  state: StreamState;
  topics: Set<string>; // topics of the stored events applied (for query invalidation)
  resync: { since_seq: number; reason: string } | null;
}

/** Apply a batch of messages in order. Stored events at or below `lastSeq` are dropped, so a
 *  replay that overlaps what we already have never duplicates anything. */
export function reduce(state: StreamState, batch: Envelope[], now = Date.now()): Applied {
  let { lastSeq, summary, quotes } = state;
  const fresh: Envelope[] = [];
  const notices: Envelope[] = [];
  const topics = new Set<string>();
  let resync: Applied["resync"] = null;
  for (const message of batch) {
    if (message.seq !== null && message.seq !== undefined) {
      if (message.seq <= lastSeq) continue;
      lastSeq = message.seq;
      fresh.push(message);
      topics.add(message.topic);
      continue;
    }
    switch (message.type) {
      case "summary":
        summary = message.data as unknown as Summary;
        break;
      case "quotes":
        quotes = (message.data as { quotes?: Record<string, QuoteView> }).quotes ?? {};
        break;
      case "subscribed": {
        // A subscription without replay starts the live tail at the server's head.
        const replayTo = Number((message.data as { replay_to?: number }).replay_to ?? 0);
        const since = Number((message.data as { since_seq?: number }).since_seq ?? 0);
        if (since >= replayTo) lastSeq = Math.max(lastSeq, replayTo);
        break;
      }
      case "resync":
        resync = {
          since_seq: Number((message.data as { since_seq?: number }).since_seq ?? 0),
          reason: String((message.data as { reason?: string }).reason ?? ""),
        };
        if (resync.reason === "new_store") lastSeq = 0;
        break;
      case "stopped":
      case "error":
        notices.push(message);
        break;
      default: // heartbeat
        break;
    }
  }
  const events = fresh.length ? [...state.events, ...fresh].slice(-KEPT) : state.events;
  return {
    state: {
      ...state,
      lastSeq,
      events,
      summary,
      quotes,
      lastMessageAt: batch.length ? now : state.lastMessageAt,
      notices: notices.length ? [...state.notices, ...notices].slice(-20) : state.notices,
    },
    topics,
    resync,
  };
}

export const useStream = create<StreamState>(() => initialStream);

// -- the connection -------------------------------------------------------------------------------

type Listener = (topics: Set<string>, resync: boolean) => void;

const TOPICS = ["events", "summary", "quotes"];
const NO_REPLAY = Number.MAX_SAFE_INTEGER;
const POLICY_VIOLATION = 1008;

/** Owns the WebSocket: connect, batch per frame, reconnect with backoff, resume. */
export class StreamClient {
  private ws: WebSocket | null = null;
  private pending: Envelope[] = [];
  private frame: number | null = null;
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private resumeFrom: number | null = null;

  constructor(
    private readonly onApplied: Listener,
    private readonly url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
  ) {}

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.ws?.close();
    this.ws = null;
  }

  private connect(): void {
    const ws = new WebSocket(this.url, wsProtocols());
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      useStream.setState({ status: "open" });
      // First connection: live only (the REST projections hold the state). After a drop:
      // replay everything after the last event we applied.
      const last = useStream.getState().lastSeq;
      const since = this.resumeFrom ?? (last > 0 ? last : NO_REPLAY);
      this.resumeFrom = null;
      ws.send(JSON.stringify({ subscribe: TOPICS, since_seq: since }));
    };
    ws.onmessage = (event) => {
      try {
        this.pending.push(JSON.parse(event.data as string) as Envelope);
      } catch {
        return;
      }
      if (this.frame === null) this.frame = requestAnimationFrame(() => this.flush());
    };
    ws.onclose = (event) => {
      if (this.ws === ws) this.ws = null;
      if (event.code === POLICY_VIOLATION) {
        useStream.setState({ status: "unauthorized" });
        return; // a wrong or expired token: retrying cannot help
      }
      if (!this.stopped) this.reconnect();
    };
  }

  private flush(): void {
    this.frame = null;
    const batch = this.pending;
    this.pending = [];
    const { state, topics, resync } = reduce(useStream.getState(), batch);
    useStream.setState(state);
    if (topics.size) this.onApplied(topics, false);
    if (resync) {
      this.resumeFrom = resync.since_seq;
      this.onApplied(new Set(), true);
      this.ws?.close(); // reconnect and re-subscribe from where we are
    }
  }

  private reconnect(): void {
    useStream.setState({ status: "reconnecting" });
    const delay = Math.min(15_000, 500 * 2 ** this.retry++);
    this.timer = setTimeout(() => this.connect(), delay);
  }
}
