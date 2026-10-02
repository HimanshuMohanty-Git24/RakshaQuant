import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, get, post } from "./client";

function respond(status: number, body: unknown) {
  return vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("sends the launch token and builds paths and query strings", async () => {
    sessionStorage.setItem("rq.token", "tok");
    const fetch = respond(200, { decision_id: "d1", exit_decision_ids: [], events: [] });
    vi.stubGlobal("fetch", fetch);
    await get("/api/decisions/{decision_id}", { params: { decision_id: "a b" } });
    await get("/api/orders", { query: { book: "A", status: undefined, limit: 50 } });
    const [first, init] = fetch.mock.calls[0]!;
    expect(first).toBe("/api/decisions/a%20b");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    expect(fetch.mock.calls[1]![0]).toBe("/api/orders?book=A&limit=50");
  });

  it("raises the server's error body", async () => {
    vi.stubGlobal("fetch", respond(422, { error: "invalid request", fields: ["body.confirm"] }));
    const error = await post("/api/risk/resume", {
      confirm: true,
      phrase: "RESUME",
      reason: "checked",
    }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 422, message: "invalid request", fields: ["body.confirm"] });
  });
});
