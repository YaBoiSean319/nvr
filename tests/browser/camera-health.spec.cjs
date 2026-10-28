// Real-browser journey for the camera health dashboard (#137): reported and unreported values, the outdated firmware flag, sorting, filtering,
// the export, and a fleet of 200 shown without any video being opened.
const { test, expect, seedFixtures, seedHealth, signInNext } = require("./helpers.cjs");

test("the dashboard shows what cameras report, says Not reported for the rest, flags older firmware, sorts, filters and exports", async ({ page }) => {
  seedFixtures(3);
  seedHealth();
  await signInNext(page);
  await page.getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("link", { name: "Health" }).click();
  const table = page.getByRole("table", { name: "Camera health" });
  const row = name => table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });

  await expect(row("Camera 001")).toContainText("Acme Cam-X1");
  await expect(row("Camera 001")).toContainText("2.10.0");
  await expect(row("Camera 001")).toContainText("4096 kbit/s set");
  await expect(row("Camera 001")).toContainText("15 fps set");
  await expect(row("Camera 001").getByRole("cell").nth(5)).toHaveText("7");  // Dropped frames: the recorder's counter
  await expect(row("Camera 001").getByRole("cell").nth(6)).toHaveText("3");  // Reconnects
  await expect(row("Camera 001").getByRole("cell").nth(2)).toContainText("1 d 6 h");
  await expect(row("Camera 003").getByRole("cell").nth(0)).toHaveText("Not reported"); // No make or model
  await expect(row("Camera 003").getByRole("cell").nth(1)).toHaveText("Not reported");
  await expect(row("Camera 003").getByRole("cell").nth(7)).toHaveText("Not reported"); // Never contacted over ONVIF

  // The one on older firmware than the other of its model is flagged, and only that one.
  await expect(page.getByTestId("fleet-outdated")).toContainText("1 camera is on older firmware");
  await expect(page.getByTestId("outdated-badge")).toHaveCount(1);
  await expect(row("Camera 002").getByTestId("outdated-badge")).toContainText("Older than 2.10.0");
  await page.getByLabel("Older firmware only").check();
  await expect(table.getByRole("row")).toHaveCount(2); // Header and Camera 002
  await page.getByLabel("Older firmware only").uncheck();

  // Sorting: by uptime, longest first, with the camera that reported none last.
  await table.getByRole("button", { name: "Recording uptime" }).click();
  await table.getByRole("button", { name: /Recording uptime/ }).click();
  const names = await table.getByRole("rowheader").allInnerTexts();
  expect(names[0]).toMatch(/^Camera 001/);
  expect(names[names.length - 1]).toMatch(/^Camera 003/);
  await page.getByLabel("Search").fill("camera 002");
  await expect(page.getByRole("status").filter({ hasText: "1 of 3 cameras" })).toBeVisible();

  // The export says the same, with "Not reported" where nothing was.
  const csv = await (await page.request.get("/api/camera-health/export.csv")).text();
  const lines = csv.trim().split("\r\n");
  expect(lines[0]).toContain("firmware_status");
  expect(lines.find(line => line.startsWith("Camera 002"))).toContain("outdated");
  expect(lines.find(line => line.startsWith("Camera 003"))).toContain("Not reported");
});

test("a fleet of 200 cameras is shown without opening any video", async ({ page }) => {
  test.setTimeout(90_000);
  seedFixtures(200);
  seedHealth();
  const media = [];
  page.on("request", request => {
    if (/\/live\/stream|\/live\?|\/media|\/snapshot|\/thumbnail/.test(request.url())) media.push(request.url());
  });
  const sockets = [];
  page.on("websocket", socket => sockets.push(socket.url()));
  await signInNext(page);
  await page.goto("/cameras/health");
  const table = page.getByRole("table", { name: "Camera health" });
  await expect(page.getByRole("status").filter({ hasText: "200 of 200 cameras" })).toBeVisible({ timeout: 20_000 });
  await expect(table.getByRole("rowheader")).toHaveCount(200);
  await page.waitForTimeout(1_500);
  expect(media).toEqual([]);
  expect(sockets.filter(url => /\/live\/stream/.test(url))).toEqual([]);
});
