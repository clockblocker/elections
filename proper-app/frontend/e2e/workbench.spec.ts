import { expect, test } from "@playwright/test";

test("loads the protocol-cloud screen, filters its display, and opens a scored protocol", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /turnout × result field/i })).toBeVisible();
  await expect(page.getByText("96,284 plotted / 96,307 protocols")).toBeVisible();
  await expect(page.getByText("DEG outside model")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Distance from the election core" })).toBeVisible();
  await expect(page.getByText("P_sus grades each complete UIK protocol", { exact: false })).toBeVisible();
  await expect(page.getByLabel("Core protocol fraction")).toHaveValue("0.5");
  await expect(page.getByLabel("P_sus review threshold")).toHaveValue("0.999");

  await page.getByLabel("Display geography").selectOption("77");
  await expect(page.getByText("3,658 plotted / 3,660 protocols")).toBeVisible();
  await expect(page.getByText("scores fixed to the all-UIK model", { exact: false })).toBeVisible();

  await expect(page.getByRole("heading", { name: "Protocol review queue" })).toBeVisible();
  await page.locator(".review-items button").first().click();
  await expect(page.getByText("Pinned physical precinct")).toBeVisible();
  const scoreCard = page.locator(".clt-card");
  await expect(scoreCard.getByText("P_sus grade")).toBeVisible();
  await expect(scoreCard.locator(".score-line strong")).toHaveText(/^P[0-3]$/);
  await expect(scoreCard.getByText("Actual", { exact: true })).toBeVisible();
  await expect(scoreCard.getByText("Expected", { exact: true })).toBeVisible();
  await expect(scoreCard.getByText("95% predictive interval")).toBeVisible();
  await expect(scoreCard.getByText("probability of fraud", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: /official source/i })).toBeVisible();
});
