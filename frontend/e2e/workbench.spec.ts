import { expect, test } from "@playwright/test";

test("reconciled UIK data survives filter reset and opens evidence", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/?party=2&region=missing-region&turnoutMin=99");

  await expect(page.getByRole("status")).toContainText("National totals reconciled");
  await expect(page.getByRole("status")).toContainText("96,325 source observations");
  await expect(
    page.getByRole("heading", { name: "No precincts match this field" }),
  ).toBeVisible();

  const firstPointPage = page.waitForResponse((response) =>
    response.url().includes("/api/v1/points?") &&
    response.url().includes("party_id=5") &&
    response.url().includes("offset=0") &&
    response.ok(),
  );
  await page.getByRole("button", { name: "Reset analytical state" }).click();
  await firstPointPage;

  await expect(
    page.getByRole("checkbox", { name: /ЕДИНАЯ РОССИЯ/i }),
  ).toBeChecked();
  await expect(page.getByText("Loading 20,000 of 96,325…")).toBeVisible();
  await expect(page.getByText("20,000 UIK–party observations")).toBeVisible();
  await expect(page.getByText("96,325 UIK–party observations")).toBeVisible();
  await expect(page.getByLabel("96,325 precinct observations. Turnout on x-axis; party result on y-axis."))
    .toBeVisible();

  await page.getByLabel("Find UIK").fill("592");
  await page.getByRole("button", { name: "Search precinct" }).click();

  await expect(page.getByRole("heading", { name: "UIK 592" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Hierarchy" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Ballot accounting" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("graph navigation stays within zero-to-one-hundred bounds", async ({ page }) => {
  await page.goto("/?party=5&turnoutMin=46&turnoutMax=47&resultMin=28&resultMax=29");

  const plot = page.locator(".plot-shell");
  await expect(plot).toBeVisible();
  await page.evaluate(() => { document.body.style.paddingBottom = "1000px"; });
  const bounds = await plot.boundingBox();
  if (!bounds) throw new Error("Plot bounds are unavailable");
  const tickLabels = plot.locator(".plot-grid g text");
  const fullDomain = ["0", "20", "40", "60", "80", "100", "0", "20", "40", "60", "80", "100"];

  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.wheel(0, 500);
  await page.waitForTimeout(100);

  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  expect(await tickLabels.allTextContents()).toEqual(fullDomain);

  await page.mouse.wheel(0, -500);
  await page.waitForTimeout(100);
  const zoomedTicks = (await tickLabels.allTextContents()).map(Number);
  expect(zoomedTicks).not.toEqual(fullDomain.map(Number));
  expect(zoomedTicks.every((tick) => tick >= 0 && tick <= 100)).toBe(true);

  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width - 5, bounds.y + bounds.height - 5);
  await page.mouse.up();
  const pannedTicks = (await tickLabels.allTextContents()).map(Number);
  expect(pannedTicks.every((tick) => tick >= 0 && tick <= 100)).toBe(true);
});
