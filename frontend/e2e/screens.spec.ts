// Plan M10.5: every screen renders on the demo replay (screenshots: RQ_SCREENSHOTS=1 writes
// them to docs/ui/screens for review).

import { expect, test } from "@playwright/test";

import { decisionId, open, shot } from "./helpers";

const SCREENS = [
  ["/", "1-command-center", "Today's decisions"],
  ["/decisions", "2-decisions", "Decisions ·"],
  ["/blotter", "4-blotter", "Blotter"],
  ["/risk", "5-risk-center", "Limits in use"],
  ["/ai", "6-ai-desk", "Roles and models"],
  ["/experiment", "7-experiment", "Daily reports"],
  ["/market?symbol=TCS", "8-market", "Announcements"],
  ["/system", "9-system", "Reconciliation (OMS vs broker)"],
] as const;

for (const [path, name, ready] of SCREENS) {
  test(`${name} renders`, async ({ page }) => {
    await open(page, path);
    await expect(page.getByText(ready).first()).toBeVisible();
    await expect(page.getByRole("note")).toContainText("DEMO"); // the persistent banner
    await page.waitForTimeout(600); // lazy charts settle
    await page.screenshot({ path: shot(name) });
  });
}

test.describe("the decision inspector, full height", () => {
  test.use({ viewport: { width: 1600, height: 2400 } });

  test("an executed and a vetoed decision", async ({ page }) => {
    await open(page);
    for (const [symbol, book, outcome, name] of [
      ["INFY", "A", "submitted", "3-inspector-executed"],
      ["TCS", "B", "vetoed", "3-inspector-vetoed"],
    ] as const) {
      await page.goto(`/decisions/${await decisionId(page, symbol, book, outcome)}`);
      await expect(page.getByText("Counterfactual (shadow ledger)")).toBeVisible();
      await page.screenshot({ path: shot(name) });
    }
  });
});
