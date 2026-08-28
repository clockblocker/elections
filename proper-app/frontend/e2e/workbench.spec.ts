import { expect, test } from "@playwright/test";

test("loads the physical field, filters a region, and opens a protocol", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /physical precinct field/i })).toBeVisible();
  await expect(page.getByText("96,284 physical UIKs")).toBeVisible();
  await expect(page.getByText("DEG outside model")).toBeVisible();

  await page.getByLabel("Analysis geography").selectOption("77");
  await expect(page.getByText("3,658 physical UIKs")).toBeVisible();

  const response = await page.request.get("/api/elections/2021-duma/points?option=5&region=77");
  const payload = await response.json() as { points: Array<{ turnout: number; result: number }> };
  const target = payload.points.find((point) => point.turnout > 55 && point.turnout < 85 && point.result > 40 && point.result < 85)!;
  const canvas = page.getByRole("img");
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  await canvas.click({ position: {
    x: 54 + target.turnout / 100 * (box!.width - 72),
    y: 20 + (100 - target.result) / 100 * (box!.height - 66)
  } });
  await expect(page.getByText("Pinned physical precinct")).toBeVisible();
  await expect(page.getByRole("link", { name: /official source/i })).toBeVisible();
});
