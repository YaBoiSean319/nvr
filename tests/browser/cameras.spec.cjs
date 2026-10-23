// Real-browser journeys for the Cameras inventory and General & Connection editor (#95, #67).
const { test, expect, seedFixtures, signInNext, trackLiveSockets, FIXTURE_PASSWORD } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const inventory = page => page.getByRole("region", { name: "Camera inventory" });
const editor = page => page.locator(".editor");

async function openCameras(page) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await expect(inventory(page)).toBeVisible();
}

function rowButton(page, name) {
  return inventory(page).getByRole("button", { name, exact: true });
}

async function selectCamera(page, name) {
  await rowButton(page, name).click();
  await expect(page.getByRole("region", { name: `Settings for ${name}` })).toBeVisible();
}

async function expectNoHorizontalOverflow(page) {
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(page.viewportSize().width);
}

/** Count requests that would start media or probe a camera. */
function trackCameraWork(page) {
  const work = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (/\/(test|profiles|probe)$/.test(url.pathname) || /^\/api\/cameras\/[^/]+\/live/.test(url.pathname)) work.push(url.pathname);
  });
  return work;
}

test("0, 1 and 200 camera inventories render without selecting anything or starting media", async ({ page }) => {
  seedFixtures(0);
  const work = trackCameraWork(page);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await openCameras(page);
  await expect(inventory(page).getByText("No cameras have been added yet.")).toBeVisible();
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();

  seedFixtures(1);
  await page.reload();
  await expect(rowButton(page, "Camera 001")).toBeVisible();
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();
  await expect(inventory(page).getByRole("row", { name: /Camera 001/ })).toContainText("Direct URL");

  seedFixtures(200);
  await page.reload();
  await expect(inventory(page).locator("tbody tr")).toHaveCount(200);
  await expect(inventory(page).getByText("200 of 200 cameras")).toBeVisible();
  await inventory(page).getByRole("searchbox").fill("192.0.2.199");
  await expect(inventory(page).locator("tbody tr")).toHaveCount(1);
  await inventory(page).getByRole("searchbox").fill("");
  await inventory(page).getByRole("button", { name: "Name" }).click();
  await expect(inventory(page).locator("tbody tr").first()).toContainText("Camera 200");
  await inventory(page).getByLabel("Policy group").selectOption("Operations");
  await expect(inventory(page).locator("tbody tr")).toHaveCount(100);
  await page.waitForTimeout(500);
  expect(work).toEqual([]);
  expect(sockets.opened()).toBe(0);
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();
});

test("a row opens exactly one editor; bulk checkboxes never change it", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await inventory(page).getByRole("checkbox", { name: "Select Camera 003 for bulk changes" }).check();
  await inventory(page).getByRole("checkbox", { name: "Select Camera 001 for bulk changes" }).check();
  await expect(editor(page)).toHaveCount(1);
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
  await expect(inventory(page).getByText("2 selected for bulk changes")).toBeVisible();
  await inventory(page).getByRole("row", { name: /Camera 003/ }).getByRole("cell").nth(3).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 003" })).toBeVisible();
  await expect(editor(page)).toHaveCount(1);
  await expect(page.getByRole("tab", { selected: true })).toHaveText("General & Connection");
});

test("dirty navigation asks Save / Discard / Cancel and saving moves on only after success", async ({ page }) => {
  const fixtures = seedFixtures(3, { failNameSave: true });
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  const name = editor(page).getByLabel("Name", { exact: true });
  await name.fill("Loading dock");

  // Cancel keeps the target and the edit.
  await rowButton(page, "Camera 003").click();
  const dialog = page.getByRole("dialog", { name: "Unsaved changes" });
  await expect(dialog).toContainText("General & Connection for Camera 002");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(name).toHaveValue("Loading dock");
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();

  // Tabs and other workspaces are guarded too.
  await page.getByRole("tab", { name: "Recording" }).click();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await nav(page).getByRole("link", { name: "Live" }).click();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(name).toHaveValue("Loading dock");

  // Save navigates after the save succeeds.
  await rowButton(page, "Camera 003").click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 003" })).toBeVisible();
  await expect(rowButton(page, "Loading dock")).toBeVisible();

  // Discard leaves the saved value alone.
  await editor(page).getByLabel("Name", { exact: true }).fill("Scratch");
  await rowButton(page, "Loading dock").click();
  await dialog.getByRole("button", { name: "Discard" }).click();
  await expect(page.getByRole("region", { name: "Settings for Loading dock" })).toBeVisible();
  const saved = await (await page.request.get("/api/cameras")).json();
  expect(saved.cameras.find(camera => camera.id === fixtures.camera_ids[2]).name).toBe("Camera 003");

  // A failing save keeps the draft and the camera.
  await rowButton(page, "Camera 001").click();
  await editor(page).getByLabel("Name", { exact: true }).fill("Rejected name");
  await rowButton(page, "Camera 003").click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 001" })).toBeVisible();
  await expect(editor(page).getByLabel("Name", { exact: true })).toHaveValue("Rejected name");
  await expect(editor(page).getByRole("alert")).toContainText("Name not saved");
  await editor(page).getByRole("button", { name: "Revert" }).click();
  await expect(editor(page).getByLabel("Name", { exact: true })).toHaveValue("Camera 001");
});

test("a stream test answer for a camera that is no longer open is ignored", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.route("**/source/test", async route => {
    await new Promise(resolve => setTimeout(resolve, 1_500));
    await route.continue().catch(() => undefined);
  });
  await editor(page).getByRole("button", { name: "Test stream" }).click();
  await selectCamera(page, "Camera 001");
  await page.waitForTimeout(2_500);
  await expect(editor(page).getByText(/Stream opened|Stream did not open/)).toHaveCount(0);
});

test("the preview starts only on request and is released on every way out", async ({ page }) => {
  seedFixtures(3);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.waitForTimeout(500);
  expect(sockets.opened()).toBe(0);

  const preview = page.getByRole("region", { name: "Preview of Camera 002" });
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect(preview).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  expect(sockets.open()).toBe(1);
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect.poll(() => sockets.open()).toBe(0);

  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await selectCamera(page, "Camera 003"); // camera change
  await expect.poll(() => sockets.open()).toBe(0);
  await expect(page.getByRole("region", { name: /Preview of/ })).toHaveCount(0);

  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await nav(page).getByRole("link", { name: "Playback" }).click(); // leaving Cameras
  await expect.poll(() => sockets.open()).toBe(0);

  await openCameras(page);
  await expect(page.getByRole("region", { name: /Preview of/ })).toHaveCount(0); // never restored
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await page.getByRole("button", { name: "Archive…" }).click(); // archive
  await page.getByRole("dialog", { name: "Archive Camera 003?" }).getByRole("button", { name: "Archive camera" }).click();
  await expect.poll(() => sockets.open()).toBe(0);
  await expect(page.getByText("Selected camera is outside the current filter.")).toBeVisible();

  await selectCamera(page, "Camera 001");
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await page.getByRole("button", { name: /Account menu/ }).click(); // sign out
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect.poll(() => sockets.open()).toBe(0);
});

test("archiving keeps the camera reachable through filters, and restore brings it back", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.getByRole("button", { name: "Archive…" }).click();
  await page.getByRole("dialog", { name: "Archive Camera 002?" }).getByRole("button", { name: "Archive camera" }).click();
  await expect(page.getByText("Selected camera is outside the current filter.")).toBeVisible();
  await expect(rowButton(page, "Camera 002")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
  await page.getByRole("button", { name: "Show in table" }).click();
  await expect(inventory(page).getByLabel("Archived cameras")).toHaveValue("all");
  await expect(inventory(page).getByRole("row", { name: /Camera 002/ })).toContainText("Archived");
  await page.getByRole("button", { name: "Restore camera" }).click();
  await expect(page.getByRole("button", { name: "Archive…" })).toBeVisible();
});

test("credentials stay out of URLs, browser storage and responses", async ({ page }) => {
  const secret = "S3cret-fixture-pass";
  seedFixtures(2);
  const urls = [];
  page.on("request", request => urls.push(request.url()));
  const bodies = [];
  page.on("response", async response => {
    if (response.url().includes("/api/")) bodies.push(await response.text().catch(() => ""));
  });
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 001");
  await editor(page).getByLabel("RTSP URL", { exact: true }).fill("rtsp://127.0.0.1:8554/nvr-test");
  await editor(page).getByLabel("Username").fill("viewer");
  await editor(page).getByLabel("Password").fill(secret);
  await editor(page).getByRole("button", { name: "Test stream" }).click();
  await expect(editor(page).locator(".field-actions").getByRole("status").or(editor(page).locator(".field-actions").getByRole("alert"))).toBeVisible({ timeout: 20_000 });
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(storage).not.toContain(secret);
  expect(urls.filter(url => url.includes(secret) || url.includes(FIXTURE_PASSWORD))).toEqual([]);
  expect(bodies.filter(body => body.includes(secret))).toEqual([]);
  const listed = await (await page.request.get("/api/cameras")).text();
  expect(listed).not.toContain(secret);
});

test("operators see read-only details and the server refuses their changes", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page, "camera-operator");
  await openCameras(page);
  await expect(page.getByRole("link", { name: "Add Cameras" })).toHaveCount(0);
  await expect(inventory(page).getByRole("checkbox", { name: /for bulk changes/ })).toHaveCount(0);
  await selectCamera(page, "Camera 002");
  await expect(editor(page).getByRole("textbox")).toHaveCount(0);
  await expect(editor(page).getByRole("button", { name: "Save" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Archive…" })).toHaveCount(0);
  await expect(editor(page).locator("dl.details")).toContainText("192.0.2.2");
  const rename = await page.request.patch(`/api/cameras/${fixtures.camera_ids[1]}`, { data: { name: "Nope" } });
  expect(rename.status()).toBe(403);
  const archive = await page.request.delete(`/api/cameras/${fixtures.camera_ids[1]}`);
  expect(archive.status()).toBe(403);
});

for (const width of [320, 768, 1440]) {
  test(`keyboard-only administration at ${width}px`, async ({ page }) => {
    seedFixtures(3);
    await page.setViewportSize({ width, height: 800 });
    await signInNext(page);
    await openCameras(page);
    await expectNoHorizontalOverflow(page);
    await inventory(page).getByRole("searchbox").focus();
    await page.keyboard.type("192.0.2");
    const target = rowButton(page, "Camera 002");
    await target.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
    const region = editor(page);
    await expectNoHorizontalOverflow(page);
    await region.getByLabel("Name", { exact: true }).focus();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type(`Gate ${width}`);
    await region.getByRole("button", { name: "Save" }).focus();
    await page.keyboard.press("Enter");
    await expect(region.getByRole("status")).toContainText("Saved name");
    await page.getByRole("tab", { name: "General & Connection" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Recording" })).toHaveAttribute("aria-selected", "true");
    if (width <= 760) {
      // The editor covers the inventory, which keeps its search and scroll underneath.
      const onTop = () => page.evaluate(() => Boolean(document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.closest(".editor-pane")));
      expect(await onTop()).toBe(true);
      await page.getByRole("button", { name: "← Back to Cameras" }).focus();
      await page.keyboard.press("Enter");
      await expect.poll(onTop).toBe(false);
      await expect(inventory(page).getByRole("searchbox")).toHaveValue("192.0.2");
      await expect(inventory(page).getByRole("row", { name: new RegExp(`Gate ${width}`) })).toHaveAttribute("aria-selected", "true");
    } else {
      const separator = page.getByRole("separator", { name: "Resize inventory and settings" });
      await separator.focus();
      const before = Number(await separator.getAttribute("aria-valuenow"));
      await page.keyboard.press("ArrowRight");
      await expect(separator).toHaveAttribute("aria-valuenow", String(before + 2));
    }
    await expectNoHorizontalOverflow(page);
  });
}
