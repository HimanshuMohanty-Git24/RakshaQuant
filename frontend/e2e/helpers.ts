import { expect, type Page } from "@playwright/test";

export const TOKEN = () => process.env.RQ_TOKEN!;

/** Open the console the way an operator does: the launch URL (token in the fragment) first. */
export async function open(page: Page, path = "/"): Promise<void> {
  await page.goto(process.env.RQ_URL!);
  await expect(page.getByText("RakshaQuant", { exact: true })).toBeVisible();
  if (path !== "/") await page.goto(path);
}

export async function api<T>(page: Page, path: string): Promise<T> {
  const res = await page.request.get(path, { headers: { Authorization: `Bearer ${TOKEN()}` } });
  expect(res.ok()).toBe(true);
  return (await res.json()) as T;
}

export async function decisionId(page: Page, symbol: string, book: string, outcome: string): Promise<string> {
  const rows = await api<{ decision_id: string }[]>(page, `/api/decisions?symbol=${symbol}&book=${book}&outcome=${outcome}`);
  expect(rows.length).toBeGreaterThan(0);
  return rows[0]!.decision_id;
}

/** Screenshots go to docs/ui/screens only when asked (RQ_SCREENSHOTS=1), else test-results. */
export function shot(name: string): string {
  return process.env.RQ_SCREENSHOTS === "1" ? `../docs/ui/screens/${name}.png` : `test-results/screens/${name}.png`;
}
