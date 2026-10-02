import { describe, expect, it } from "vitest";

import {
  bps,
  dateIST,
  direction,
  duration,
  inr,
  inrCompact,
  pct,
  price,
  qty,
  ratio,
  relative,
  symbolOf,
  timeIST,
} from "./format";

describe("money", () => {
  it("groups INR the Indian way with two decimals", () => {
    expect(inr("123456.78")).toBe("₹1,23,456.78");
    expect(inr(10004210)).toBe("₹1,00,04,210.00");
    expect(inr("0")).toBe("₹0.00");
  });
  it("always signs losses with a real minus, gains only on request", () => {
    expect(inr("-1820")).toBe("−₹1,820.00");
    expect(inr(1820, { sign: true })).toBe("+₹1,820.00");
    expect(inr(0, { sign: true })).toBe("₹0.00");
    expect(inr(-0.004)).toBe("−₹0.00");
  });
  it("shows a dash for missing values, never NaN", () => {
    for (const v of [null, undefined, "", "abc"]) expect(inr(v)).toBe("—");
  });
  it("compacts in thousands, lakh and crore", () => {
    expect(inrCompact(4100)).toBe("₹4.1k");
    expect(inrCompact(1004210)).toBe("₹10.04L");
    expect(inrCompact(-12_000_000, { sign: true })).toBe("−₹1.20Cr");
    expect(inrCompact(710, { sign: true })).toBe("+₹710");
  });
});

describe("numbers", () => {
  it("formats prices, percents, ratios, bps and quantities with fixed decimals", () => {
    expect(price("1512.4")).toBe("1,512.40");
    expect(pct(0.18, { sign: true })).toBe("+0.18%");
    expect(pct("-1.5")).toBe("−1.50%");
    expect(ratio(0.0412)).toBe("4.12%");
    expect(bps(12.345, { sign: true })).toBe("+12.3 bps");
    expect(qty(123456)).toBe("1,23,456");
    expect(qty(-42)).toBe("−42");
  });
  it("gives a direction for colour (never the only signal)", () => {
    expect(direction("12.5")).toBe("up");
    expect(direction(-1)).toBe("down");
    expect(direction(0)).toBe("flat");
    expect(direction(null)).toBe("flat");
  });
});

describe("time", () => {
  it("renders IST regardless of the host time zone", () => {
    expect(timeIST("2026-10-05T04:12:13Z")).toBe("09:42:13");
    expect(timeIST("2026-10-05T09:42:13+05:30")).toBe("09:42:13");
    expect(dateIST("2026-10-05T20:00:00Z")).toBe("Tue 06 Oct"); // already the next day in IST
  });
  it("treats a bare date as a calendar date", () => {
    expect(dateIST("2026-10-05")).toBe("Mon 05 Oct");
  });
  it("gives relative hints and durations", () => {
    const now = Date.parse("2026-10-05T04:12:25Z");
    expect(relative("2026-10-05T04:12:13Z", now)).toBe("12 s ago");
    expect(relative("2026-10-05T04:02:13Z", now)).toBe("10 min ago");
    expect(duration(930)).toBe("15m 30s");
    expect(duration(7_260)).toBe("2h 1m");
  });
  it("reduces instrument keys to symbols", () => {
    expect(symbolOf("NSE:EQ:INFY")).toBe("INFY");
  });
});
