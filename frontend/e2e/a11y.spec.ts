// §6.7: axe on every screen - zero serious or critical violations (WCAG 2.x A/AA rules).

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { decisionId, open } from "./helpers";

const PATHS = ["/", "/decisions", "/blotter", "/risk", "/ai", "/experiment", "/market?symbol=TCS", "/system"];

async function scan(page: import("@playwright/test").Page) {
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  return result.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id}: ${v.help} (${v.nodes.length}) e.g. ${v.nodes[0]?.target.join(" ")}`);
}

for (const path of PATHS) {
  test(`axe: ${path}`, async ({ page }) => {
    await open(page, path);
    await page.waitForTimeout(600);
    expect(await scan(page)).toEqual([]);
  });
}

test("axe: the inspector and a dialog", async ({ page }) => {
  await open(page);
  await page.goto(`/decisions/${await decisionId(page, "TCS", "B", "vetoed")}`);
  await expect(page.getByText("Counterfactual (shadow ledger)")).toBeVisible();
  expect(await scan(page)).toEqual([]);
  await page.getByRole("banner").getByRole("button", { name: "HALT" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await scan(page)).toEqual([]);
});
