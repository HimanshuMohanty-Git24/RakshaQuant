import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { invalidator } from "../api/queries";
import { sessionDay } from "./TopBar";

describe("projection invalidation", () => {
  afterEach(() => vi.useRealTimers());

  it("refetches what a topic changes, at most once per interval", () => {
    vi.useFakeTimers();
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    const onApplied = invalidator(client, 1_000);
    onApplied(new Set(["orders"]), false);
    onApplied(new Set(["orders", "risk"]), false);
    expect(spy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1_000);
    const prefixes = spy.mock.calls.map((c) => (c[0] as { queryKey: string[] }).queryKey[0]);
    expect(new Set(prefixes)).toEqual(new Set(["orders", "fills", "positions", "summary", "decision", "risk"]));
    expect(prefixes).toHaveLength(6); // each once
  });

  it("refetches everything on a resync", () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    invalidator(client)(new Set(), true);
    expect(spy).toHaveBeenCalledWith();
  });
});

describe("the session day", () => {
  it("is the IST date of the session clock", () => {
    expect(sessionDay("2026-10-05T20:00:00Z")).toBe("2026-10-06");
    expect(sessionDay("2026-10-05T03:45:00+00:00")).toBe("2026-10-05");
  });
});
