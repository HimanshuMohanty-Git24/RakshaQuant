// §6.7: 60 fps scrolling on a 5,000-row blotter. The rows are served by the browser (a mocked
// /api/decisions), so the measurement is the table's rendering, not the server.

import { expect, test } from "@playwright/test";

import { open } from "./helpers";

test("a 5,000-row table scrolls without dropping frames", async ({ page }) => {
  const rows = Array.from({ length: 5000 }, (_, i) => ({
    seq: i + 1,
    ts: new Date(Date.UTC(2026, 9, 5, 3, 50, i % 60)).toISOString(),
    decision_id: `d${String(i).padStart(10, "0")}`,
    signal_id: `momentum:NSE:EQ:S${i}:2026-10-01`,
    book_id: ["A", "B", "C"][i % 3],
    instrument_key: `NSE:EQ:S${i}`,
    symbol: `S${i}`,
    strategy: "momentum",
    disposition: i % 7 ? "submitted" : "vetoed",
    detail: i % 7 ? "" : "advisor veto",
    client_order_id: null,
  }));
  await page.route(/\/api\/decisions\?/, (route) => route.fulfill({ json: rows }));
  await open(page, "/decisions");
  const table = page.getByRole("table", { name: "Decisions" });
  await expect(table).toHaveAttribute("aria-rowcount", "5001");
  expect(await table.getByRole("row").count()).toBeLessThan(80); // virtualised

  const frames = await table.evaluate(async (el) => {
    const intervals: number[] = [];
    let last = performance.now();
    const end = last + 3000;
    await new Promise<void>((done) => {
      const step = (now: number) => {
        intervals.push(now - last);
        last = now;
        el.scrollTop += 40; // ~1.7 rows per frame, continuously
        if (now < end && el.scrollTop + el.clientHeight < el.scrollHeight) requestAnimationFrame(step);
        else done();
      };
      requestAnimationFrame(step);
    });
    return intervals.slice(1);
  });
  frames.sort((a, b) => a - b);
  const median = frames[Math.floor(frames.length / 2)]!;
  const p95 = frames[Math.floor(frames.length * 0.95)]!;
  console.log(`frames ${frames.length}, median ${median.toFixed(1)} ms, p95 ${p95.toFixed(1)} ms`);
  expect(median).toBeLessThan(20); // ~60 fps (16.7 ms), with headroom for timer jitter
  expect(p95).toBeLessThan(34); // no sustained jank: at worst a dropped frame
});
