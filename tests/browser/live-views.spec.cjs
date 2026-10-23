// Real-browser journeys for Live views and arrangement (#93).
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

function row(page, name) {
  return page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
}

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}:`) });
}

async function add(page, ...indexes) {
  for (const index of indexes) await row(page, `Camera ${String(index).padStart(3, "0")}`).click();
}

async function displayed(page) {
  return page.locator(".grid-cell:not([hidden]) .tile").evaluateAll(tiles =>
    tiles
      .map(tile => ({ name: tile.querySelector(".tile-name").textContent, position: Number(tile.parentElement.dataset.position) }))
      .sort((a, b) => a.position - b.position)
      .map(entry => entry.name));
}

async function views(page) {
  const response = await page.request.get("/api/live/views");
  const body = await response.json();
  return [...body.layouts, ...body.shared_views];
}

test("saving views is explicit: loading never changes a view and changes show until saved", async ({ page }) => {
  const fixtures = seedFixtures(4);
  await signInNext(page);
  await add(page, 1, 2);
  await page.getByRole("button", { name: "Save as…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save view as" });
  await dialog.getByLabel("Name").fill("Dock");
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(page.locator(".view-name")).toContainText("Dock");
  await expect(page.getByText("Unsaved changes")).toHaveCount(0);

  await add(page, 3);
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  expect((await views(page)).find(view => view.name === "Dock").camera_ids).toHaveLength(2);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Unsaved changes")).toHaveCount(0);
  expect((await views(page)).find(view => view.name === "Dock").camera_ids).toEqual(fixtures.camera_ids.slice(0, 3));

  // Rearranging after loading marks the view dirty again; the saved copy is untouched.
  await page.getByRole("tab", { name: "Views" }).click();
  await row(page, "Dock").click();
  await expect(page.locator(".grid-cell")).toHaveCount(3);
  await tile(page, "Camera 003").getByRole("button", { name: "Remove Camera 003 from view" }).click();
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  expect((await views(page)).find(view => view.name === "Dock").camera_ids).toHaveLength(3);
});

test("operators use shared views but cannot change them, and the server agrees", async ({ page, browser }) => {
  const fixtures = seedFixtures(3);
  await signInNext(page);
  await add(page, 1, 3);
  await page.getByRole("button", { name: "Save as…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save view as" });
  await dialog.getByLabel("Name").fill("Front desk");
  await dialog.getByLabel("Everyone (shared)").check();
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(page.locator(".view-name")).toContainText("Shared view");

  const context = await browser.newContext();
  const operator = await context.newPage();
  await signInNext(operator, "camera-operator");
  await operator.getByRole("tab", { name: "Views" }).click();
  await row(operator, "Front desk").click();
  await expect(operator.locator(".grid-cell")).toHaveCount(2);
  await tile(operator, "Camera 001").getByRole("button", { name: "Remove Camera 001 from view" }).click();
  await expect(operator.getByText("Unsaved changes")).toBeVisible();
  await expect(operator.getByRole("button", { name: "Save", exact: true })).toHaveCount(0);
  await expect(operator.getByRole("button", { name: "Delete view" })).toHaveCount(0);
  await operator.getByRole("button", { name: "Save as…" }).click();
  await expect(operator.getByRole("dialog").getByLabel("Everyone (shared)")).toHaveCount(0);
  await operator.keyboard.press("Escape");

  const layout = { camera_ids: [fixtures.camera_ids[1]], grid_size: 1, tile_camera_ids: [fixtures.camera_ids[1]], layout_mode: "auto" };
  expect((await operator.request.put("/api/live/shared-layouts/Front%20desk", { data: layout })).status()).toBe(403);
  expect((await operator.request.delete("/api/live/shared-layouts/Front%20desk")).status()).toBe(403);
  await context.close();
});

test("a group larger than a view opens a subset chooser instead of truncating", async ({ page }) => {
  seedFixtures(40); // odd-numbered cameras (20) belong to Operations
  await signInNext(page);
  await page.getByRole("tab", { name: "Groups" }).click();
  await row(page, "Operations").click();
  const chooser = page.getByRole("dialog", { name: "Choose cameras from Operations" });
  await expect(chooser).toBeVisible();
  await expect(chooser.getByRole("checkbox")).toHaveCount(20);
  await expect(chooser.getByText("16/16 chosen")).toBeVisible();
  await chooser.getByRole("checkbox", { name: "Camera 001" }).uncheck();
  await chooser.getByRole("checkbox", { name: "Camera 039" }).check();
  await chooser.getByRole("button", { name: "Show 16 cameras" }).click();
  await expect(page.locator(".grid-cell")).toHaveCount(16);
  await expect(tile(page, "Camera 039")).toBeVisible();
  await expect(tile(page, "Camera 001")).toHaveCount(0);
});

test("dragging replaces or swaps without duplicating, with keyboard alternatives", async ({ page }) => {
  seedFixtures(4);
  await signInNext(page);
  await add(page, 1, 2, 3);
  expect(await displayed(page)).toEqual(["Camera 001", "Camera 002", "Camera 003"]);

  await row(page, "Camera 004").dragTo(tile(page, "Camera 002"));
  expect(await displayed(page)).toEqual(["Camera 001", "Camera 004", "Camera 003"]);

  await tile(page, "Camera 001").dragTo(tile(page, "Camera 003"));
  expect(await displayed(page)).toEqual(["Camera 003", "Camera 004", "Camera 001"]);

  await tile(page, "Camera 003").focus();
  await page.keyboard.press("Alt+ArrowRight");
  expect(await displayed(page)).toEqual(["Camera 004", "Camera 003", "Camera 001"]);

  await tile(page, "Camera 001").getByRole("button", { name: "More actions for Camera 001" }).click();
  await page.getByRole("menuitem", { name: "Replace with…" }).click();
  const chooser = page.getByRole("dialog", { name: "Replace Camera 001" });
  await chooser.getByRole("button", { name: /^Camera 002/ }).click();
  expect(await displayed(page)).toEqual(["Camera 004", "Camera 003", "Camera 002"]);
  await expect(page.locator(".grid-cell")).toHaveCount(3);
});

test("manual layouts keep positions and fall back to Auto when full", async ({ page }) => {
  seedFixtures(10);
  await signInNext(page);
  await add(page, 1, 2);
  await page.getByRole("button", { name: "Layout" }).click();
  await page.getByRole("menuitemradio", { name: "3 × 3" }).click();
  await expect(page.locator(".grid")).toHaveAttribute("data-mode", "manual");
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "3");
  await expect(page.getByText("Empty position")).toHaveCount(7);

  await tile(page, "Camera 001").getByRole("button", { name: "Remove Camera 001 from view" }).click();
  await expect(page.getByText("Empty position")).toHaveCount(8);
  expect(await page.locator(".grid-cell", { has: page.locator(".tile") }).getAttribute("data-position")).toBe("1");

  await add(page, 3, 4, 5, 6, 7, 8, 9, 10);
  await expect(page.locator(".grid")).toHaveAttribute("data-mode", "manual");
  await add(page, 1);
  await expect(page.getByText(/returned to Auto/)).toBeVisible();
  await expect(page.locator(".grid")).toHaveAttribute("data-mode", "auto");
  await expect(page.locator(".grid-cell")).toHaveCount(10);
});

test("the Live workspace comes back after refresh and a new sign-in, always muted", async ({ page, browser }) => {
  seedFixtures(3, { streams: { 2: "rtsp://127.0.0.1:8554/nvr-audio" }, audio: [2] });
  await signInNext(page);
  await add(page, 3, 2);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await tile(page, "Camera 002").getByRole("button", { name: "Listen to Camera 002" }).click();
  await page.getByRole("button", { name: "Hide camera browser" }).click();
  await page.waitForTimeout(1_000); // workspace saves after a short pause

  await page.reload();
  await expect(page.getByRole("button", { name: "Show camera browser" })).toBeVisible();
  await expect.poll(() => displayed(page)).toEqual(["Camera 003", "Camera 002"]);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await expect.poll(() => tile(page, "Camera 002").locator("video").evaluate(video => video.muted)).toBe(true);

  const context = await browser.newContext();
  const again = await context.newPage();
  await signInNext(again);
  await expect.poll(() => displayed(again)).toEqual(["Camera 003", "Camera 002"]);
  await context.close();
});

test("views saved by the earlier interface still load", async ({ page }) => {
  const fixtures = seedFixtures(3);
  await signInNext(page);
  const legacy = await page.request.put("/api/live/layouts/Legacy%20wall", {
    data: { camera_ids: [fixtures.camera_ids[2], fixtures.camera_ids[0]] },
  });
  expect(legacy.ok()).toBeTruthy();
  await page.reload();
  await page.getByRole("tab", { name: "Views" }).click();
  await row(page, "Legacy wall").click();
  expect(await displayed(page)).toEqual(["Camera 003", "Camera 001"]);
});
