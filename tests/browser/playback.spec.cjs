// Real-browser journeys for the Playback core (#94, #77) on generated footage.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const VIEWPORTS = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
];

function row(page, name) {
  return page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
}

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}.*: playback$`) });
}

async function openPlayback(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await expect(page.getByRole("form", { name: "Footage search" })).toBeVisible();
}

async function add(page, ...indexes) {
  for (const index of indexes) await row(page, `Camera ${String(index).padStart(3, "0")}`).click();
}

async function clockTime(page) {
  return Number(await page.getByLabel("Playback time").getAttribute("data-time"));
}

async function search(page) {
  await page.getByRole("button", { name: "Search", exact: true }).click();
}

/** Absolute source time each playing tile shows: interval start + the video's position. */
async function tileTimes(page) {
  return page.locator(".playback-tile[data-segment-start]").evaluateAll(tiles =>
    tiles.map(tile => Number(tile.dataset.segmentStart) + tile.querySelector("video").currentTime * 1000));
}

/** Everything a person needs is on screen and the document itself never scrolls. */
async function expectFitsViewport(page) {
  const viewport = page.viewportSize();
  const scroll = await page.evaluate(() => ({
    height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth,
  }));
  expect(scroll.height, "document scrolls vertically").toBeLessThanOrEqual(viewport.height);
  expect(scroll.width, "document scrolls horizontally").toBeLessThanOrEqual(viewport.width);
  const regions = {
    navigation: page.getByRole("navigation", { name: "Workspaces" }),
    video: page.locator(".playback-video"),
    search: page.getByRole("form", { name: "Footage search" }),
    transport: page.getByRole("toolbar", { name: "Playback controls" }),
    timeline: page.getByRole("region", { name: "Timeline" }),
  };
  for (const [name, locator] of Object.entries(regions)) {
    const box = await locator.boundingBox();
    expect(box, `${name} is rendered`).not.toBeNull();
    expect(box.y, `${name} top`).toBeGreaterThanOrEqual(0);
    expect(box.x, `${name} left`).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height, `${name} bottom`).toBeLessThanOrEqual(viewport.height + 0.5);
    expect(box.x + box.width, `${name} right`).toBeLessThanOrEqual(viewport.width + 0.5);
  }
  const video = await regions.video.boundingBox();
  expect(video.height, "video stays the dominant region").toBeGreaterThan(viewport.height * 0.3);
}

test("the workspace fits one viewport for 0, 1, 4, 9 and 16 cameras with long names", async ({ page }) => {
  test.setTimeout(180_000);
  seedFixtures(16, { footage: true, longNames: true });
  await page.setViewportSize(VIEWPORTS[0]);
  await signInNext(page);
  await openPlayback(page);
  await search(page);
  let selected = 0;
  for (const count of [0, 1, 4, 9, 16]) {
    while (selected < count) await add(page, ++selected);
    await expect(page.locator(".playback-tile")).toHaveCount(count);
    await expect(page.locator(".timeline-row[data-camera-id]")).toHaveCount(count);
    for (const viewport of VIEWPORTS) {
      await page.setViewportSize(viewport);
      await expectFitsViewport(page);
    }
  }
  // Many rows scroll inside the timeline pane, not the page.
  const body = page.locator(".timeline-body");
  expect(await body.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);

  // Collapsing the browser and resizing the window recompute the grid without clipping.
  const before = await page.locator(".playback-video").boundingBox();
  await page.getByRole("button", { name: "Hide camera browser" }).click();
  await expect.poll(async () => (await page.locator(".playback-video").boundingBox()).width).toBeGreaterThan(before.width);
  await expectFitsViewport(page);
  await page.setViewportSize({ width: 1600, height: 820 });
  await expectFitsViewport(page);
  const grid = await page.locator(".playback-video").boundingBox();
  for (const box of await page.locator(".playback-tile").evaluateAll(tiles => tiles.map(tile => tile.getBoundingClientRect().toJSON()))) {
    expect(box.bottom).toBeLessThanOrEqual(grid.y + grid.height + 0.5);
    expect(box.right).toBeLessThanOrEqual(grid.x + grid.width + 0.5);
  }
});

test("narrow windows use collapsible panes and local scrolling", async ({ page }) => {
  seedFixtures(4, { footage: true });
  await page.setViewportSize({ width: 700, height: 760 });
  await signInNext(page);
  await openPlayback(page);
  await add(page, 1, 2, 3, 4);
  await search(page);
  await page.getByRole("button", { name: "Hide camera browser" }).click();
  await expect(page.getByRole("button", { name: "Show camera browser" })).toBeVisible();
  const scroll = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth }));
  expect(scroll.width).toBeLessThanOrEqual(700);
  expect(scroll.height).toBeLessThanOrEqual(760);
  await page.getByRole("button", { name: "Hide timeline" }).click();
  await expect(page.getByRole("region", { name: "Timeline" })).toHaveCount(0);
  await page.getByRole("button", { name: "Show timeline" }).click();
  await expect(page.getByRole("region", { name: "Timeline" })).toBeVisible();

  // Phone width: panes stack and scroll inside the workspace, never sideways.
  await page.setViewportSize({ width: 360, height: 640 });
  const narrow = await page.evaluate(() => ({ height: document.documentElement.scrollHeight, width: document.documentElement.scrollWidth }));
  expect(narrow.width).toBeLessThanOrEqual(360);
  expect(narrow.height).toBeLessThanOrEqual(640);
  await page.getByRole("toolbar", { name: "Playback controls" }).scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeInViewport();
});

test("search starts paused at the earliest recording and marks cameras without footage", async ({ page }) => {
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await openPlayback(page);
  await add(page, 2, 3);
  await search(page);
  // Camera 002's footage starts 30 s after the anchor; camera 003 has none.
  await expect.poll(() => clockTime(page)).toBe(anchor + 30_000);
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await expect(tile(page, "Camera 003")).toHaveAttribute("data-phase", "no-footage");
  await expect(tile(page, "Camera 003").getByText("No footage in this range")).toBeVisible();
  await expect(page.locator(`.timeline-row[data-camera-id="${fixtures.camera_ids[2]}"]`)).toHaveAttribute("data-footage", "none");
  await expect(page.locator(`.timeline-row[data-camera-id="${fixtures.camera_ids[1]}"] [data-mark="ai"]`)).toHaveCount(1);
  await expect(page.locator(`.timeline-row[data-camera-id="${fixtures.camera_ids[1]}"] [data-mark="unanalyzed"]`)).toHaveCount(1);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });

  // Clicking the timeline seeks; nothing plays until asked.
  const ruler = page.getByRole("slider", { name: "Playback position" });
  const box = await ruler.boundingBox();
  await ruler.click({ position: { x: 2, y: box.height / 2 } });
  expect(await clockTime(page)).toBeLessThan(anchor);
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
});

test("changed inputs are marked stale until applied, and the 24-hour bound is explained", async ({ page }) => {
  seedFixtures(1, { footage: true });
  await signInNext(page);
  await openPlayback(page);
  await add(page, 1);
  await search(page);
  const form = page.getByRole("form", { name: "Footage search" });
  await expect(form).toHaveAttribute("data-stale", "false");
  const from = form.getByLabel("From", { exact: true });
  const original = await from.inputValue();
  await from.fill("2026-01-01T00:00");
  await expect(form).toHaveAttribute("data-stale", "true");
  await expect(form.getByText("Search changed — press Search to apply")).toBeVisible();
  await search(page);
  await expect(form.getByRole("alert")).toHaveText("A search covers at most 24 hours. Narrow the range.");
  await from.fill(original);
  await expect(form).toHaveAttribute("data-stale", "false");
});

test("repeated and skipped daylight-saving times are chosen or rejected explicitly", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const saved = await page.request.put("/api/settings/display", { data: { time_zone: "America/New_York", date_format: "iso", clock_format: "24" } });
  expect(saved.ok()).toBeTruthy();
  await openPlayback(page);
  await add(page, 1);
  const form = page.getByRole("form", { name: "Footage search" });

  // 01:30 happens twice on 2026-11-01 in New York.
  await form.getByLabel("From", { exact: true }).fill("2026-11-01T01:30");
  await form.getByLabel("To", { exact: true }).fill("2026-11-01T03:00");
  const occurrence = form.getByLabel("Which From occurrence");
  await expect(occurrence.locator("option")).toHaveText(["Choose occurrence…", "First · GMT-4", "Second · GMT-5"]);
  await search(page);
  await expect(form.getByRole("alert")).toContainText("happens twice in America/New_York");
  await occurrence.selectOption({ label: "Second · GMT-5" });
  await search(page);
  await expect(form.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Timeline" })).toHaveAttribute("data-window-start", String(Date.parse("2026-11-01T06:30:00Z")));
  await expect(page.getByLabel("Playback time")).toHaveText("2026-11-01 01:30:00");

  // 02:30 never happens on 2026-03-08 in New York.
  await form.getByLabel("From", { exact: true }).fill("2026-03-08T02:30");
  await form.getByLabel("To", { exact: true }).fill("2026-03-08T04:00");
  await search(page);
  await expect(form.getByRole("alert").last()).toContainText("does not exist in America/New_York");
});

test("every tile follows one clock across speeds and gaps show No recording while time advances", async ({ page }) => {
  test.setTimeout(90_000);
  const fixtures = seedFixtures(2, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await openPlayback(page);
  await add(page, 1, 2);
  await search(page);
  await expect.poll(() => clockTime(page)).toBe(anchor);
  // Start where both cameras have footage.
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  for (let step = 0; step < 7; step += 1) await page.keyboard.press("ArrowRight"); // +35 s
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });

  for (const [speed, tolerance] of [["1", 600], ["4", 1500]]) {
    await page.getByLabel("Speed").selectOption(speed);
    await page.getByRole("button", { name: "Play", exact: true }).click();
    await page.waitForTimeout(2_000);
    const [clock, times] = await Promise.all([clockTime(page), tileTimes(page)]);
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    expect(times).toHaveLength(2);
    for (const time of times) expect(Math.abs(time - clock), `tile drift at ${speed}×`).toBeLessThan(tolerance + Number(speed) * 100);
  }

  // Camera 001 has a failure gap from +60 s to +150 s; the clock keeps moving through it.
  await page.getByLabel("Speed").selectOption("16");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "no-recording", { timeout: 10_000 });
  await expect(tile(page, "Camera 001").getByText("No recording: Camera offline")).toBeVisible();
  // The timeline names the same reason on hover.
  await expect(page.locator('.timeline-mark[data-reason="camera_offline"]').first()).toHaveAttribute("title", /Recording failure: Camera offline\./);
  const inGap = await clockTime(page);
  await expect.poll(() => clockTime(page)).toBeGreaterThan(inGap + 5_000);
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(page.locator(`.timeline-row[data-camera-id="${fixtures.camera_ids[0]}"] [data-mark="failure"]`)).toHaveCount(1);
});

test("adding a camera mid-investigation keeps time, play state and the zoomed timeline", async ({ page }) => {
  const fixtures = seedFixtures(3, { footage: true });
  await signInNext(page);
  await openPlayback(page);
  await add(page, 1);
  await search(page);
  await expect.poll(() => clockTime(page)).toBe(Date.parse(fixtures.footage.anchor));
  await page.getByRole("button", { name: "Zoom in" }).click();
  await page.getByRole("button", { name: "Zoom in" }).click();
  const timeline = page.getByRole("region", { name: "Timeline" });
  const window = [await timeline.getAttribute("data-window-start"), await timeline.getAttribute("data-window-end")];
  await expect(page.locator(".timeline-row [data-mark='motion']")).toHaveCount(2);
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.waitForTimeout(1_000);
  const before = await clockTime(page);

  await add(page, 2);
  await expect(tile(page, "Camera 002")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await expect.poll(() => clockTime(page)).toBeGreaterThan(before);
  expect(await clockTime(page)).toBeLessThan(before + 5_000);
  await expect(timeline).toHaveAttribute("data-window-start", window[0]);
  await expect(timeline).toHaveAttribute("data-window-end", window[1]);
  await expect(page.locator(".timeline-row [data-mark='motion']")).toHaveCount(4);
  await expect(page.locator(".timeline-row[data-camera-id]")).toHaveCount(2);

  await page.getByRole("button", { name: "Fit range" }).click();
  await expect(page.getByRole("button", { name: "Fit range" })).toBeDisabled();
});

test("Playback remembers its own investigation separately from Live", async ({ page }) => {
  const fixtures = seedFixtures(3, { footage: true });
  await signInNext(page);
  await openPlayback(page);
  await add(page, 1, 3);
  await search(page);
  await expect.poll(() => clockTime(page)).toBe(Date.parse(fixtures.footage.anchor));
  await page.getByLabel("Speed").selectOption("4");
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  await page.keyboard.press("Shift+ArrowRight");
  const time = await clockTime(page);
  expect(time).toBe(Date.parse(fixtures.footage.anchor) + 60_000);
  await page.waitForTimeout(1_000);

  await page.reload();
  await expect(tile(page, "Camera 001")).toBeVisible();
  await expect(tile(page, "Camera 003")).toBeVisible();
  await expect.poll(() => clockTime(page)).toBe(time);
  await expect(page.getByLabel("Speed")).toHaveValue("4");
  await expect(page.getByRole("form", { name: "Footage search" })).toHaveAttribute("data-stale", "false");

  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Live" }).click();
  await expect(page.locator(".grid-cell")).toHaveCount(0);
});
