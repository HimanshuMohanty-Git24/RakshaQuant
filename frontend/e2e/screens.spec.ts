import { expect, test, type Page } from "@playwright/test";

const shots = "test-results/screens";

async function open(page: Page, path = "/") {
  await page.goto(process.env.RQ_URL!); // carries the token once; the app stores it
  await expect(page.getByText("RakshaQuant", { exact: true })).toBeVisible();
  if (path !== "/") await page.goto(path);
}

async function decision(page: Page, symbol: string, book: string, outcome: string): Promise<string> {
  const res = await page.request.get(`/api/decisions?symbol=${symbol}&book=${book}&outcome=${outcome}`, {
    headers: { Authorization: `Bearer ${process.env.RQ_TOKEN}` },
  });
  const rows = (await res.json()) as { decision_id: string }[];
  expect(rows.length).toBeGreaterThan(0);
  return rows[0]!.decision_id;
}

test("command center", async ({ page }) => {
  await open(page);
  await expect(page.getByRole("table", { name: "Books" })).toBeVisible();
  await expect(page.getByText("Today's decisions")).toBeVisible();
  await page.screenshot({ path: `${shots}/command.png` });
});

test("decisions and the inspector", async ({ page }) => {
  await open(page, "/decisions");
  await expect(page.getByRole("table", { name: "Decisions" })).toBeVisible();
  await page.screenshot({ path: `${shots}/decisions.png` });
  const executed = await decision(page, "INFY", "A", "submitted");
  await page.goto(`/decisions/${executed}`);
  await expect(page.getByText("Orders and fills")).toBeVisible();
  await page.screenshot({ path: `${shots}/inspector-executed.png`, fullPage: true });
  const vetoed = await decision(page, "TCS", "B", "vetoed");
  await page.goto(`/decisions/${vetoed}`);
  await expect(page.getByText("VETO").first()).toBeVisible();
  await page.screenshot({ path: `${shots}/inspector-vetoed.png`, fullPage: true });
});

test("blotter", async ({ page }) => {
  await open(page, "/blotter");
  await page.getByRole("tab", { name: "Trades" }).click();
  await expect(page.getByRole("table", { name: "Trades" })).toBeVisible();
  await page.screenshot({ path: `${shots}/blotter.png` });
});

for (const [path, name, ready] of [
  ["/risk", "risk", "Limits in use"],
  ["/ai", "ai", "Roles and models"],
  ["/experiment", "experiment", "Daily reports"],
  ["/market?symbol=TCS", "market", "Announcements"],
  ["/system", "system", "Reconciliation (OMS vs broker)"],
] as const) {
  test(`${name} screen`, async ({ page }) => {
    await open(page, path);
    await expect(page.getByText(ready, { exact: false }).first()).toBeVisible();
    await page.waitForTimeout(500); // lazy charts
    await page.screenshot({ path: `${shots}/${name}.png` });
  });
}
