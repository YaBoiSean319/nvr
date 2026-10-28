// Real-browser journey for Insights (#135): counts per camera, unanalysed time marked in the chart and the CSV, and the heatmap on the fixture paths.
const { test, expect, seedFixtures, signInNext, seedSearchTracks } = require("./helpers.cjs");

test("Insights counts what the tracks show, marks the hours nobody analysed, draws the heatmap on the walked path and exports the same as CSV", async ({ page }) => {
  seedFixtures(3);
  seedSearchTracks(); // Camera 1: two walkers across the middle of the frame and a failed hour of analysis; camera 2: a truck; camera 3: one walker, never analysed.
  await signInNext(page);
  await page.getByRole("link", { name: "Insights" }).click();
  await expect(page.getByRole("heading", { name: "Insights" })).toBeVisible();

  const totals = page.getByRole("table", { name: "Totals for the period" });
  const row = name => totals.locator("tr", { has: page.getByRole("button", { name }) });
  await expect(row("Camera 001").locator('[data-label="person"]')).toHaveText("2");
  await expect(row("Camera 001").locator('[data-label="vehicle"]')).toHaveText("0");
  await expect(row("Camera 002").locator('[data-label="vehicle"]')).toHaveText("1");
  await expect(row("Camera 003").locator('[data-label="person"]')).toHaveText("1");
  await expect(page.getByTestId("insights-unanalysed")).toContainText("not analysing all of this time");

  // Camera 1 was analysed except for one failed hour, which touches one or two clock hours; all of camera 3's hours are marked.
  const marks = page.getByTestId("unanalysed-mark");
  await expect(page.getByTestId("insights-chart")).toBeVisible();
  const first = await marks.count();
  expect(first).toBeGreaterThanOrEqual(1);
  expect(first).toBeLessThanOrEqual(2);
  const bars = page.getByTestId("insights-chart").locator("g[data-total]");
  const peopleHours = await bars.evaluateAll(groups => groups.filter(group => Number(group.dataset.total) > 0).length);
  expect(peopleHours).toBe(2); // Two walkers, three and two hours ago
  await row("Camera 003").getByRole("button", { name: "Camera 003" }).click();
  await expect(marks).toHaveCount(24);

  // The CSV says the same, hour by hour.
  const href = await page.getByRole("link", { name: "Download CSV" }).getAttribute("href");
  const csv = await (await page.request.get(href)).text();
  const lines = csv.trim().split("\r\n");
  expect(lines[0]).toBe("bucket_start_local,bucket_start_utc,kind,name,camera,person,vehicle,analysed_percent,note");
  const camera3 = lines.filter(line => line.includes(",Camera 003,"));
  expect(camera3).toHaveLength(24);
  expect(camera3.every(line => line.includes("Not fully analysed"))).toBe(true);
  const camera1 = lines.filter(line => line.includes(",camera,Camera 001,"));
  expect(camera1.filter(line => line.includes("Not fully analysed")).length).toBe(first);

  // The heatmap of camera 1 concentrates on the row the walkers crossed, at the two ends of their path.
  await page.getByLabel("Heatmap camera").selectOption({ label: "Camera 001" });
  await expect(page.getByTestId("heatmap")).toBeVisible();
  await expect(page.getByTestId("heat-cell").first()).toBeAttached();
  const cells = await page.getByTestId("heat-cell").evaluateAll(rects => rects.map(rect => ({ x: Number(rect.getAttribute("x")), y: Number(rect.getAttribute("y")), count: Number(rect.dataset.count) })));
  expect(cells.length).toBeGreaterThanOrEqual(2);
  expect(new Set(cells.map(cell => cell.y))).toEqual(new Set([7]));
  expect(cells.every(cell => cell.x <= 8 || cell.x >= 23)).toBe(true);
  await expect(page.getByText("There is no recording to show behind it.")).toBeVisible();
});
