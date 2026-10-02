import { expect, test } from "@playwright/test";

test("the shell renders on the demo replay, behind the token", async ({ page }) => {
  await page.goto(process.env.RQ_URL!);
  await expect(page.getByText("RakshaQuant", { exact: true })).toBeVisible();
  await expect(page.getByRole("note")).toContainText("DEMO");
  await expect(page).not.toHaveURL(/token=/); // stripped from the address bar
  await page.screenshot({ path: "test-results/shell.png" });
});
