import { describe, expect, it } from "vitest";

import { initialStream, reduce, type Envelope } from "./stream";

const event = (seq: number, type = "Alert", topic = "system"): Envelope => ({
  v: 1,
  seq,
  type,
  topic,
  ts: "2026-10-05T04:00:00Z",
  decision_id: null,
  book_id: null,
  data: { key: `k${seq}` },
});
const notice = (type: string, data: Record<string, unknown>, topic = "system"): Envelope => ({
  v: 1,
  seq: null,
  type,
  topic,
  ts: "2026-10-05T04:00:00Z",
  decision_id: null,
  book_id: null,
  data,
});

describe("the stream reducer", () => {
  it("applies stored events once, in seq order, across an overlapping replay", () => {
    let { state } = reduce(initialStream, [event(1), event(2), event(3)]);
    const replay = reduce(state, [event(2), event(3), event(4, "OrderAcked", "orders")]);
    state = replay.state;
    expect(state.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(state.lastSeq).toBe(4);
    expect([...replay.topics]).toEqual(["orders"]);
  });

  it("keeps the latest slot values and starts a live-only subscription at the head", () => {
    const { state } = reduce(initialStream, [
      notice("subscribed", { since_seq: 812, replay_to: 812 }),
      notice("summary", { last_seq: 812, demo: true }, "summary"),
      notice("summary", { last_seq: 815, demo: true }, "summary"),
      notice("quotes", { quotes: { "NSE:EQ:INFY": { symbol: "INFY", ltp: 1528.1 } } }, "quotes"),
      notice("heartbeat", { last_seq: 815 }),
    ]);
    expect(state.lastSeq).toBe(812);
    expect(state.summary).toMatchObject({ last_seq: 815 });
    expect(state.quotes["NSE:EQ:INFY"]?.ltp).toBe(1528.1);
    expect(state.events).toEqual([]);
  });

  it("asks for a resync, and starts over for a new store", () => {
    const base = reduce(initialStream, [event(10)]).state;
    const overflow = reduce(base, [notice("resync", { since_seq: 10, reason: "overflow" })]);
    expect(overflow.resync).toEqual({ since_seq: 10, reason: "overflow" });
    expect(overflow.state.lastSeq).toBe(10);
    const restarted = reduce(base, [notice("resync", { since_seq: 0, reason: "new_store" })]);
    expect(restarted.state.lastSeq).toBe(0);
  });

  it("keeps run notices and only a bounded history", () => {
    const many = Array.from({ length: 700 }, (_, i) => event(i + 1));
    const { state } = reduce(initialStream, [...many, notice("stopped", {})]);
    expect(state.events).toHaveLength(600);
    expect(state.events[0]?.seq).toBe(101);
    expect(state.notices.map((n) => n.type)).toEqual(["stopped"]);
  });
});
