// Real-browser journeys for Playback investigation (#97) on generated footage.
// Fixture offsets from the anchor (seconds): camera 1 (15 fps) records 0-60 and 150-210 with
// motion at 40 and 160; camera 2 (10 fps) records 30-90 with motion at 40 and 75.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const FRAME_15 = 1000 / 15;
const FRAME_10 = 100;

function row(page, name) {
  return page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
}

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}.*: playback$`) });
}

function liveTile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}:`) });
}

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

async function clockTime(page) {
  return Number(await page.getByLabel("Playback time").getAttribute("data-time"));
}

/** Investigate cameras 1 and 2 over the default past hour; the clock starts at the anchor. */
async function investigate(page, cameras = [1, 2]) {
  const fixtures = seedFixtures(Math.max(3, ...cameras), { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await nav(page).getByRole("link", { name: "Playback" }).click();
  for (const index of cameras) await row(page, `Camera ${String(index).padStart(3, "0")}`).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => clockTime(page)).toBe(anchor);
  return { fixtures, anchor };
}

/** Move the playhead with the timeline's keyboard control (±5 s per press). */
async function seekBy(page, seconds) {
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  const key = seconds < 0 ? "ArrowLeft" : "ArrowRight";
  for (let step = 0; step < Math.abs(seconds) / 5; step += 1) await ruler.press(key);
}

function offsetFrom(time, start, frame) {
  const remainder = (time - start) % frame;
  return Math.min(remainder, frame - remainder);
}

test("frame stepping lands on each camera's real frame timestamps, across gaps", async ({ page }) => {
  const { anchor } = await investigate(page);
  const controls = page.getByRole("toolbar", { name: "Playback controls" });
  await seekBy(page, 35);

  // Camera 002 records at 10 fps from +30 s.
  await tile(page, "Camera 002").click();
  await controls.getByRole("button", { name: "Next frame" }).click();
  await expect.poll(() => clockTime(page)).toBeGreaterThan(anchor + 35_000);
  let time = await clockTime(page);
  expect(time - (anchor + 35_000)).toBeLessThanOrEqual(FRAME_10);
  expect(offsetFrom(time, anchor + 30_000, FRAME_10)).toBeLessThanOrEqual(1);

  // Stepping while playing pauses on a real frame.
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.waitForTimeout(300);
  await controls.getByRole("button", { name: "Next frame" }).click();
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await expect.poll(async () => offsetFrom(await clockTime(page), anchor + 30_000, FRAME_10)).toBeLessThanOrEqual(1);
  time = await clockTime(page);

  // Camera 001 records at 15 fps from the anchor.
  await tile(page, "Camera 001").click();
  await controls.getByRole("button", { name: "Next frame" }).click();
  await expect.poll(() => clockTime(page)).toBeGreaterThan(time);
  time = await clockTime(page);
  expect(offsetFrom(time, anchor, FRAME_15)).toBeLessThanOrEqual(1);
  await controls.getByRole("button", { name: "Previous frame" }).click();
  await expect.poll(() => clockTime(page)).toBeLessThan(time);
  expect(Math.abs(time - (await clockTime(page)) - FRAME_15)).toBeLessThanOrEqual(1);

  // From inside camera 001's gap: next is the first frame after it, previous the last before it.
  await seekBy(page, 65); // ≈ +100 s
  await controls.getByRole("button", { name: "Next frame" }).click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 150_000);
  await seekBy(page, -50); // ≈ +100 s
  await controls.getByRole("button", { name: "Previous frame" }).click();
  await expect.poll(async () => Math.abs((await clockTime(page)) - (anchor + 60_000 - FRAME_15))).toBeLessThanOrEqual(1);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });
});

test("event navigation merges simultaneous starts, skips the current one, never wraps and keeps play state", async ({ page }) => {
  const { anchor } = await investigate(page);
  const next = page.getByRole("button", { name: "Event ▶" });
  const previous = page.getByRole("button", { name: "◀ Event" });
  const status = page.getByRole("group", { name: "Investigation" }).getByRole("status");

  await next.click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 40_000); // cameras 1 and 2 at once
  await next.click(); // skips the event under the playhead
  await expect.poll(() => clockTime(page)).toBe(anchor + 75_000);
  await next.click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 160_000);
  await next.click();
  await expect(status).toContainText("No later event in this search.");
  expect(await clockTime(page)).toBe(anchor + 160_000);
  await previous.click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 75_000);

  // Active camera only.
  await tile(page, "Camera 002").click();
  await page.getByLabel("Events from").selectOption("active");
  await next.click();
  await expect(status).toContainText("No later event for Camera 002 in this search.");
  await previous.click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 40_000);

  // Playing stays playing; Shift+arrows are the keyboard shortcuts.
  await page.getByLabel("Events from").selectOption("all");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.locator("body").press("Shift+ArrowRight");
  await expect.poll(() => clockTime(page)).toBeGreaterThanOrEqual(anchor + 75_000);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
});

test("Skip Empty Periods skips only time empty on every searched camera", async ({ page }) => {
  const { anchor } = await investigate(page);
  await page.getByLabel("Skip empty periods").check();

  // 0-30 s has footage on camera 001 only, so it plays normally.
  await seekBy(page, 20);
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.waitForTimeout(1_500);
  let time = await clockTime(page);
  expect(time).toBeGreaterThan(anchor + 20_000);
  expect(time).toBeLessThan(anchor + 24_000);
  await page.getByRole("button", { name: "Pause", exact: true }).click();

  // 90-150 s is empty on both cameras and is skipped.
  await seekBy(page, 65); // ≈ +86 s
  await page.getByRole("button", { name: "Play", exact: true }).click();
  const seen = [];
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    time = await clockTime(page);
    seen.push(time);
    if (time > anchor + 151_000) break;
    await page.waitForTimeout(100);
  }
  expect(time).toBeGreaterThan(anchor + 150_000);
  expect(seen.filter(value => value > anchor + 91_000 && value < anchor + 150_000)).toEqual([]);
});

test("snapshots use the active camera's displayed time and are refused in gaps", async ({ page }) => {
  await investigate(page);
  const snapshots = [];
  page.on("request", request => request.url().includes("/snapshot?") && snapshots.push(request.url()));
  await tile(page, "Camera 001").click();
  await seekBy(page, 100); // camera 001 has no recording at +100 s
  await page.getByRole("button", { name: "Snapshot" }).click();
  const status = page.getByRole("group", { name: "Investigation" }).getByRole("status");
  await expect(status).toContainText("Camera 001 has no recording at");
  expect(snapshots).toEqual([]);

  await seekBy(page, -90); // +10 s
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Snapshot" }).click();
  expect((await download).suggestedFilename()).toMatch(/^Camera_001_.*\.jpg$/);
  await expect(status).toContainText(/Snapshot of Camera 001 at .* saved \(audit-\d+\)\./);
  expect(snapshots).toHaveLength(1);
});

test("an older frame answer never overrides a newer one, and scrubbing settles on the last position", async ({ page }) => {
  const { anchor } = await investigate(page, [1]);
  await seekBy(page, 10);
  let first = true;
  await page.route("**/frame-neighbor?*", async route => {
    if (first) {
      first = false;
      await new Promise(resolve => setTimeout(resolve, 1_500));
    }
    await route.continue().catch(() => undefined);
  });
  const controls = page.getByRole("toolbar", { name: "Playback controls" });
  await controls.getByRole("button", { name: "Next frame" }).click(); // slow answer
  await controls.getByRole("button", { name: "Previous frame" }).click(); // fast answer
  await expect.poll(() => clockTime(page)).toBeLessThan(anchor + 10_000);
  const settled = await clockTime(page);
  await page.waitForTimeout(2_000);
  expect(await clockTime(page)).toBe(settled);
  expect(Math.abs(settled - (anchor + 10_000 - FRAME_15))).toBeLessThanOrEqual(1);

  // Scrub quickly back and forth across camera 001's first recording, stopping at +30 s:
  // the tile ends up showing the final position, not an earlier seek.
  const ruler = page.getByRole("slider", { name: "Playback position" });
  for (let zoom = 0; zoom < 3; zoom += 1) await page.getByRole("button", { name: "Zoom in" }).click();
  const timeline = page.getByRole("region", { name: "Timeline" });
  const [windowStart, windowEnd] = [Number(await timeline.getAttribute("data-window-start")), Number(await timeline.getAttribute("data-window-end"))];
  const box = await ruler.boundingBox();
  const xFor = time => box.x + ((time - windowStart) / (windowEnd - windowStart)) * box.width;
  const y = box.y + box.height / 2;
  await page.mouse.move(xFor(anchor + 5_000), y);
  await page.mouse.down();
  for (const seconds of [50, 8, 45, 12, 55, 20, 40, 30]) await page.mouse.move(xFor(anchor + seconds * 1000), y, { steps: 3 });
  await page.mouse.up();
  const final = await clockTime(page);
  expect(Math.abs(final - (anchor + 30_000))).toBeLessThan((windowEnd - windowStart) / box.width * 2);
  const shown = () => tile(page, "Camera 001").evaluate(element =>
    Number(element.dataset.segmentStart) + element.querySelector("video").currentTime * 1000);
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "ready", { timeout: 10_000 });
  await expect.poll(async () => Math.abs((await shown()) - final), { timeout: 10_000 }).toBeLessThan(150);
  await page.waitForTimeout(1_000);
  expect(Math.abs((await shown()) - final)).toBeLessThan(150);
});

test("shortcuts work on the page and are suppressed while typing", async ({ page }) => {
  const { anchor } = await investigate(page);
  const from = page.getByLabel("From", { exact: true });
  await from.focus();
  await from.press("ArrowRight");
  await from.press("Space");
  expect(await clockTime(page)).toBe(anchor);
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();

  await page.locator(".playback-video").click({ position: { x: 2, y: 2 } });
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => clockTime(page)).toBe(anchor + 5_000);
  await page.keyboard.press("Space");
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await page.keyboard.press("Space");
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
});

test("Open in Playback starts a past-hour investigation, then joins it keeping time", async ({ page }) => {
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await row(page, "Camera 001").click();
  await liveTile(page, "Camera 001").getByRole("button", { name: "More actions for Camera 001" }).click();
  await page.getByRole("menuitem", { name: "Open in Playback" }).click();
  await expect(page).toHaveURL(/\/playback$/);
  await expect(tile(page, "Camera 001")).toBeVisible();
  await expect.poll(() => clockTime(page)).toBe(anchor);
  await expect(page.getByRole("form", { name: "Footage search" })).toHaveAttribute("data-stale", "false");
  await seekBy(page, 20);

  await nav(page).getByRole("link", { name: "Live" }).click();
  await row(page, "Camera 002").click();
  await liveTile(page, "Camera 002").getByRole("button", { name: "More actions for Camera 002" }).click();
  await page.getByRole("menuitem", { name: "Open in Playback" }).click();
  await expect(tile(page, "Camera 002")).toBeVisible();
  await expect(tile(page, "Camera 001")).toBeVisible();
  expect(await clockTime(page)).toBe(anchor + 20_000);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-active", "true");
});

test("Open in Playback asks before replacing a camera when Playback is full", async ({ page }) => {
  test.setTimeout(90_000);
  const fixtures = seedFixtures(17, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await nav(page).getByRole("link", { name: "Playback" }).click();
  for (let index = 1; index <= 16; index += 1) await row(page, `Camera ${String(index).padStart(3, "0")}`).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => clockTime(page)).toBe(anchor);
  await seekBy(page, 10);

  const openSeventeen = async first => {
    await nav(page).getByRole("link", { name: "Live" }).click();
    if (first) await row(page, "Camera 017").click();
    await expect(liveTile(page, "Camera 017")).toBeVisible();
    await liveTile(page, "Camera 017").getByRole("button", { name: "More actions for Camera 017" }).click();
    await page.getByRole("menuitem", { name: "Open in Playback" }).click();
    return page.getByRole("dialog", { name: "Playback is full" });
  };

  let dialog = await openSeventeen(true);
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".playback-tile")).toHaveCount(16);
  await expect(tile(page, "Camera 017")).toHaveCount(0);

  dialog = await openSeventeen(false);
  await dialog.getByRole("button", { name: "Replace Camera 003" }).click();
  await expect(tile(page, "Camera 017")).toBeVisible();
  await expect(tile(page, "Camera 003")).toHaveCount(0);
  await expect(page.locator(".playback-tile")).toHaveCount(16);
  expect(await clockTime(page)).toBe(anchor + 10_000);
});
