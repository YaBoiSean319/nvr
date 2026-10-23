const fs = require("node:fs");
const path = require("node:path");
const { test, expect, seedFixtures, seedPlaybackFixtures, signIn, openCameraSettings } = require("./helpers.cjs");

const VIEWPORTS = [
  { width: 1920, height: 1080 },
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
];
const CAMERA_COUNTS = [0, 1, 2, 4, 9, 16];

function localDateTime(iso) {
  return iso.replace(/\+00:00$/, "").replace(/Z$/, "");
}

async function openPlayback(page) {
  await page.locator('#console-nav [data-screen="footage"]').click();
  await expect(page.locator("#playback-search")).toBeVisible();
}

async function signInToPlayback(page) {
  await signIn(page);
  await openPlayback(page);
}

async function selectSidebarCameras(page, count) {
  const cameraButtons = page.locator("#sidebar-cameras button");
  await expect(cameraButtons).toHaveCount(count);
  const selected = page.locator('#sidebar-cameras button[aria-label^="Remove "]');
  while (await selected.count()) await selected.first().click();
  for (let index = 0; index < count; index += 1) await cameraButtons.nth(index).click();
  await expect(page.locator("[data-playback-camera]:checked")).toHaveCount(count);
}

async function captureGeometry(page, expectedCount, viewport, expectedColumns = "4") {
  const geometry = await page.evaluate(() => {
    const rect = selector => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const box = element.getBoundingClientRect();
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: box.width, height: box.height };
    };
    const grid = document.querySelector("#playback-grid");
    const rows = document.querySelector("#playback-timeline-rows");
    const longName = [...document.querySelectorAll(".playback-camera h4")].find(element => element.textContent.includes("Fixture Camera 16"));
    return {
      viewport: { width: innerWidth, height: innerHeight },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      search: rect("#playback-search-row"),
      media: rect("#playback-grid"),
      transport: rect("#playback-transport-row"),
      timeline: rect("#playback-timeline-panel"),
      mediaTileCount: document.querySelectorAll(".playback-camera").length,
      columns: grid?.style.getPropertyValue("--playback-grid-columns") || "",
      timelineRows: rows?.querySelectorAll(".playback-timeline-row").length || 0,
      timelineScroll: rows ? { scrollHeight: rows.scrollHeight, clientHeight: rows.clientHeight } : null,
      longName: longName ? { title: longName.title, clipped: longName.scrollWidth > longName.clientWidth } : null,
    };
  });

  expect(geometry.document.width, `document width with ${expectedCount} camera(s) at ${viewport.width}×${viewport.height}`).toBeLessThanOrEqual(viewport.width);
  expect(geometry.document.height, `document height with ${expectedCount} camera(s) at ${viewport.width}×${viewport.height}`).toBeLessThanOrEqual(viewport.height);
  expect(geometry.mediaTileCount).toBe(expectedCount);
  for (const [name, box] of Object.entries({ search: geometry.search, media: geometry.media, transport: geometry.transport, timeline: geometry.timeline })) {
    expect(box, `${name} is mounted`).not.toBeNull();
    expect(box.left, `${name} left edge`).toBeGreaterThanOrEqual(0);
    expect(box.right, `${name} right edge`).toBeLessThanOrEqual(viewport.width);
    expect(box.top, `${name} top edge`).toBeGreaterThanOrEqual(0);
    expect(box.bottom, `${name} bottom edge`).toBeLessThanOrEqual(viewport.height);
  }
  if (expectedCount === 1) expect(geometry.media.width).toBeGreaterThan(viewport.width * 0.55);
  if (expectedCount === 16) {
    expect(geometry.columns).toBe(expectedColumns);
    expect(geometry.timelineRows).toBe(16);
    expect(geometry.timelineScroll.scrollHeight).toBeGreaterThan(geometry.timelineScroll.clientHeight);
  }
  return geometry;
}

for (const viewport of VIEWPORTS) {
  test(`playback fits ${viewport.width}×${viewport.height} for 0/1/2/4/9/16 cameras`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize(viewport);
    seedFixtures(0);
    await signInToPlayback(page);
    const measurement = { viewport, counts: [] };

    for (const count of CAMERA_COUNTS) {
      const fixture = seedFixtures(count, { longCameraName: count === 16 });
      await page.reload();
      await expect(page.locator("#status")).toBeVisible();
      await openPlayback(page);
      await selectSidebarCameras(page, count);

      if (count > 0) {
        await page.locator("#playback-search").click();
        await expect(page.locator(".playback-camera")).toHaveCount(count);
        await expect(page.locator(".playback-camera video").first()).toBeVisible();
      } else {
        await expect(page.locator("#playback-empty")).toBeVisible();
        await page.locator("#playback-search").click();
        await expect(page.locator("#playback-status")).toContainText("Choose between one and sixteen");
      }

      if (count === 1) {
        const from = await page.locator("#playback-start").inputValue();
        const to = await page.locator("#playback-end").inputValue();
        await page.locator("#playback-end").fill(from);
        await page.locator("#playback-search").click();
        await expect(page.locator("#playback-status")).toContainText("end time must be later");
        await page.locator("#playback-end").fill(to);
        await page.locator("#playback-search").click();
        await expect(page.locator(".playback-camera")).toHaveCount(1);
      }

      const geometry = await captureGeometry(page, count, viewport);
      if (count === 16) {
        const longTitle = "Fixture Camera 16 · North Loading Dock Perimeter Camera for Overnight Receiving";
        await expect(page.locator(".playback-camera h4").filter({ hasText: "Fixture Camera 16" })).toHaveAttribute("title", longTitle);
        expect(geometry.longName.title).toBe(longTitle);
        expect(geometry.longName.clipped).toBe(true);
      }
      measurement.counts.push(geometry);

      if (count === 16 && viewport.width === 1366) {
        const widthBefore = await page.locator("#playback-grid").evaluate(element => element.getBoundingClientRect().width);
        await page.locator("#sidebar-toggle").click();
        await expect(page.locator("#console-layout")).toHaveClass(/sidebar-collapsed/);
        await expect.poll(() => page.locator("#playback-grid").evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThan(widthBefore);

        await page.setViewportSize({ width: 900, height: 768 });
        await expect.poll(() => page.locator("#playback-grid").evaluate(element => element.style.getPropertyValue("--playback-grid-columns"))).toBe("2");
        const resized = await page.evaluate(() => ({
          viewport: { width: innerWidth, height: innerHeight },
          document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
          grid: (() => { const box = document.querySelector("#playback-grid").getBoundingClientRect(); return { right: box.right, bottom: box.bottom }; })(),
        }));
        expect(resized.document.width).toBeLessThanOrEqual(resized.viewport.width);
        expect(resized.document.height).toBeLessThanOrEqual(resized.viewport.height);
        expect(resized.grid.right).toBeLessThanOrEqual(resized.viewport.width);
        expect(resized.grid.bottom).toBeLessThanOrEqual(resized.viewport.height);
        await page.setViewportSize(viewport);
        await expect.poll(() => page.locator("#playback-grid").evaluate(element => element.style.getPropertyValue("--playback-grid-columns"))).toBe("4");

        // An 80% CSS viewport is equivalent to a 125% desktop browser zoom.
        const zoomViewport = { width: 1024, height: 640 };
        await page.setViewportSize(zoomViewport);
        await expect.poll(() => page.locator("#playback-grid").evaluate(element => element.style.getPropertyValue("--playback-grid-columns"))).toBe("2");
        await captureGeometry(page, count, zoomViewport, "2");
        await page.setViewportSize(viewport);
      }
    }

    const evidencePath = path.join("test-results", `playback-viewport-${viewport.width}x${viewport.height}.json`);
    fs.mkdirSync(path.dirname(evidencePath), { recursive: true, mode: 0o700 });
    fs.writeFileSync(evidencePath, `${JSON.stringify(measurement, null, 2)}\n`, { mode: 0o600 });

    if (viewport.width === 1366) {
      await page.locator('#console-nav [data-screen="app-settings"]').click();
      await expect(page.locator("#app-settings-content")).toBeVisible();
      await page.locator('[data-settings-category="appearance"]').click();
      const outsidePlayback = await page.evaluate(() => ({
        playbackLock: document.querySelector("#status").classList.contains("playback-screen-active"),
        documentHeight: document.documentElement.scrollHeight,
        viewportHeight: innerHeight,
      }));
      expect(outsidePlayback.playbackLock).toBe(false);
      expect(outsidePlayback.documentHeight).toBeGreaterThan(outsidePlayback.viewportHeight);
      await page.mouse.wheel(0, 700);
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    }
  });
}

test("archived cameras stay in the secondary chooser", async ({ page }) => {
  const fixture = seedFixtures(1, { archivedPlayback: true });
  await signInToPlayback(page);
  expect(await page.locator("#playback-advanced").evaluate(element => element.open)).toBe(false);
  await page.locator("#playback-advanced summary").click();
  const archived = page.locator(`[data-playback-camera][value="${fixture.archived_camera_id}"]`);
  await expect(page.locator("#playback-camera-picker label").filter({ has: archived })).toContainText("archived");
  await archived.check();
  await page.locator("#sidebar-cameras button").first().click();
  await page.locator("#playback-search").click();
  await expect(page.locator(".playback-camera")).toHaveCount(2);
  await expect(page.locator(".playback-timeline-row")).toHaveCount(2);
  await expect(page.locator(".playback-camera h4")).toContainText("Archived Fixture Camera");
});

test("sidebar group and saved-view browsing select Playback cameras", async ({ page }) => {
  const fixture = seedPlaybackFixtures(4, { savedPlaybackView: true });
  await signInToPlayback(page);

  await page.locator("#sidebar-groups button").filter({ hasText: "Operations" }).click();
  await expect(page.locator("[data-playback-camera]:checked")).toHaveCount(2);
  expect(await page.locator("[data-playback-camera]:checked").evaluateAll(inputs => inputs.map(input => input.value).sort()))
    .toEqual([fixture.camera_ids[0], fixture.camera_ids[2]].sort());

  const savedView = page.locator("#sidebar-views button").filter({ hasText: "Playback Fixture View" });
  await expect(savedView).toBeVisible();
  await savedView.click();
  await expect(page.locator("[data-playback-camera]:checked")).toHaveCount(2);
  expect(await page.locator("[data-playback-camera]:checked").evaluateAll(inputs => inputs.map(input => input.value).sort()))
    .toEqual(fixture.camera_ids.slice(0, 2).sort());
  await expect(page.locator("#playback-grid-size")).toHaveValue("4");
  await expect(page.locator("#playback-status")).toContainText("Applied personal view");
});

test("200-camera navigation scrolls inside the sidebar and Playback keeps both themes bounded", async ({ page }) => {
  const viewport = { width: 1366, height: 768 };
  seedFixtures(200);
  await page.setViewportSize(viewport);
  await signInToPlayback(page);
  await expect(page.locator("#sidebar-cameras button")).toHaveCount(200);

  const geometry = await page.evaluate(() => {
    const sidebar = document.querySelector("#console-sidebar");
    return {
      viewport: { width: innerWidth, height: innerHeight },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      sidebar: { height: sidebar.clientHeight, scrollHeight: sidebar.scrollHeight, overflowY: getComputedStyle(sidebar).overflowY },
    };
  });
  expect(geometry.document.width).toBeLessThanOrEqual(viewport.width);
  expect(geometry.document.height).toBeLessThanOrEqual(viewport.height);
  expect(geometry.sidebar.scrollHeight).toBeGreaterThan(geometry.sidebar.height);
  expect(["auto", "scroll"]).toContain(geometry.sidebar.overflowY);
  await page.locator("#sidebar-cameras button").last().scrollIntoViewIfNeeded();
  await expect.poll(() => page.locator("#console-sidebar").evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);

  await page.locator("#theme-choice").selectOption("light");
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim())).toBe("#f4f6f8");
  await page.locator("#theme-choice").selectOption("dark");
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--bg").trim())).toBe("#0d1117");
  const themedDocument = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }));
  expect(themedDocument.width).toBeLessThanOrEqual(viewport.width);
  expect(themedDocument.height).toBeLessThanOrEqual(viewport.height);
});

test("playback controls, marks, drawer state and export download stay within one investigation", async ({ page }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  const fixture = seedPlaybackFixtures(1, { playbackJobs: true });
  await signInToPlayback(page);
  await page.locator("#sidebar-cameras button").first().click();
  await page.locator("#playback-start").fill(localDateTime(fixture.playback.start));
  await page.locator("#playback-end").fill(localDateTime(fixture.playback.end));
  await page.locator("#playback-search").click();

  const video = page.locator(".playback-camera video");
  await expect(video).toHaveCount(1);
  await page.waitForFunction(() => document.querySelector(".playback-camera video")?.readyState >= 2, null, { timeout: 15_000 });
  await expect.poll(() => video.evaluate(element => element.videoWidth)).toBe(320);
  await expect.poll(() => video.evaluate(element => element.duration)).toBeGreaterThan(10);
  await expect(page.locator("#playback-timeline-rows .playback-timeline-row")).toHaveCount(1);

  const eventTime = Date.parse(fixture.playback.start) + 5000;
  await page.locator("#playback-next-event").click();
  await expect.poll(() => page.locator("#playback-cursor").getAttribute("data-time")).toBe(String(eventTime));
  const nextFrame = page.waitForResponse(response => response.url().includes("/frame-neighbor?") && response.url().includes("direction=next"));
  await page.locator("#playback-next-frame").click();
  expect((await nextFrame).ok()).toBe(true);
  const previousFrame = page.waitForResponse(response => response.url().includes("/frame-neighbor?") && response.url().includes("direction=previous"));
  await page.locator("#playback-prev-frame").click();
  expect((await previousFrame).ok()).toBe(true);

  await page.locator(".playback-camera h4").click();
  await page.keyboard.press("i");
  const markIn = await page.locator("#export-start").inputValue();
  await page.keyboard.press("ArrowRight");
  const steppedTime = Number(await page.locator("#playback-cursor").getAttribute("data-time"));
  expect(steppedTime).toBeGreaterThan(eventTime);
  await page.keyboard.press("o");
  const markOut = await page.locator("#export-end").inputValue();
  expect(markOut).not.toBe(markIn);

  await page.locator("#export-end").fill(markIn);
  await expect(page.locator("#export-preflight")).toContainText("Export end must be later");
  await page.locator("#export-end").fill(markOut);
  await expect(page.locator("#playback-export")).toBeEnabled();
  await page.locator("#playback-rate").selectOption("2");
  await page.locator(".playback-camera h4").click();
  await page.keyboard.press("ArrowLeft");
  await page.locator("#playback-play").click();
  await expect.poll(() => video.evaluate(element => element.paused)).toBe(false);
  await expect.poll(() => video.evaluate(element => element.playbackRate)).toBe(2);

  const sourceBefore = await video.getAttribute("src");
  const marksBefore = [markIn, markOut];
  const searchBefore = [await page.locator("#playback-start").inputValue(), await page.locator("#playback-end").inputValue()];
  const playbackSnapshot = () => page.evaluate(() => {
    const box = element => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height };
    };
    const activeVideo = document.querySelector(".playback-camera video");
    return {
      sampledAt: performance.now(),
      cursorTime: Number(document.querySelector("#playback-cursor").dataset.time),
      videoTime: activeVideo.currentTime,
      paused: activeVideo.paused,
      grid: box(document.querySelector("#playback-grid")),
      tiles: [...document.querySelectorAll(".playback-camera")].map(tile => ({ tile: box(tile), video: box(tile.querySelector("video")) })),
    };
  });
  const assertPlaybackAndLayoutContinued = (before, after, label) => {
    expect(after.paused, `${label}: playback remains active`).toBe(false);
    expect(after.cursorTime, `${label}: shared playhead does not move backwards`).toBeGreaterThanOrEqual(before.cursorTime);
    expect(after.videoTime, `${label}: media playhead does not move backwards`).toBeGreaterThanOrEqual(before.videoTime);
    const cursorAdvance = after.cursorTime - before.cursorTime;
    const videoAdvance = (after.videoTime - before.videoTime) * 1000;
    expect(Math.abs(cursorAdvance - videoAdvance), `${label}: shared and media playheads advance together`).toBeLessThan(750);
    const elapsedMs = after.sampledAt - before.sampledAt;
    expect(Math.abs(cursorAdvance - elapsedMs * 2), `${label}: playhead keeps the selected 2× playback rate`).toBeLessThan(800);
    const cursorMediaDifference = Math.abs(after.cursorTime - (Date.parse(fixture.playback.start) + after.videoTime * 1000));
    expect(cursorMediaDifference, `${label}: shared playhead remains aligned with the generated source video`).toBeLessThan(1_500);
    expect(after.grid, `${label}: playback grid geometry is unchanged`).toEqual(before.grid);
    expect(after.tiles, `${label}: tile count and tile/video bounding boxes are unchanged`).toEqual(before.tiles);
  };
  const beforeDrawer = await playbackSnapshot();
  await page.evaluate(() => { window.playbackVideoBeforeExport = document.querySelector(".playback-camera video"); });
  await page.locator("#playback-export-open").click();
  const dialog = page.locator("#playback-export-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#playback-export-close")).toBeFocused();
  await expect(page.locator("#export-jobs li")).toHaveCount(12);
  const jobList = await page.locator("#export-jobs").evaluate(element => ({ scrollHeight: element.scrollHeight, clientHeight: element.clientHeight }));
  expect(jobList.scrollHeight).toBeGreaterThan(jobList.clientHeight);
  const drawerGeometry = await page.evaluate(() => {
    const dialogBox = document.querySelector("#playback-export-dialog").getBoundingClientRect();
    return {
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      viewport: { width: innerWidth, height: innerHeight },
      dialog: { left: dialogBox.left, top: dialogBox.top, right: dialogBox.right, bottom: dialogBox.bottom },
    };
  });
  expect(drawerGeometry.document.width).toBeLessThanOrEqual(drawerGeometry.viewport.width);
  expect(drawerGeometry.document.height).toBeLessThanOrEqual(drawerGeometry.viewport.height);
  expect(drawerGeometry.dialog.left).toBeGreaterThanOrEqual(0);
  expect(drawerGeometry.dialog.top).toBeGreaterThanOrEqual(0);
  expect(drawerGeometry.dialog.right).toBeLessThanOrEqual(drawerGeometry.viewport.width);
  expect(drawerGeometry.dialog.bottom).toBeLessThanOrEqual(drawerGeometry.viewport.height);
  expect(await page.evaluate(() => document.querySelector(".playback-camera video") === window.playbackVideoBeforeExport)).toBe(true);
  await expect(video).toHaveAttribute("src", sourceBefore);
  await expect(video).not.toHaveJSProperty("paused", true);
  await expect(page.locator("[data-export-camera]:checked")).toHaveCount(1);
  await expect(page.locator("#export-start")).toHaveValue(marksBefore[0]);
  await expect(page.locator("#export-end")).toHaveValue(marksBefore[1]);
  await expect(page.locator("#playback-start")).toHaveValue(searchBefore[0]);
  await expect(page.locator("#playback-end")).toHaveValue(searchBefore[1]);
  const drawerOpen = await playbackSnapshot();
  assertPlaybackAndLayoutContinued(beforeDrawer, drawerOpen, "opening export drawer");
  await page.locator('[data-export-mode="grid"]').uncheck();
  await page.locator("#playback-export-close").click();
  await expect(page.locator("#playback-export-open")).toBeFocused();
  expect(await page.evaluate(() => document.querySelector(".playback-camera video") === window.playbackVideoBeforeExport)).toBe(true);
  await expect(video).toHaveAttribute("src", sourceBefore);
  await expect(video).not.toHaveJSProperty("paused", true);
  const drawerClosed = await playbackSnapshot();
  assertPlaybackAndLayoutContinued(drawerOpen, drawerClosed, "closing export drawer");

  await page.locator("#playback-pause").click();
  await expect.poll(() => video.evaluate(element => element.paused)).toBe(true);
  await page.locator("#playback-export-open").click();
  const exportResponse = page.waitForResponse(response => response.url().endsWith("/api/exports") && response.request().method() === "POST");
  await page.locator("#playback-export").click();
  const response = await exportResponse;
  expect(response.status()).toBe(202);
  const job = await response.json();
  const downloadLink = page.locator(`#export-jobs a[href="/api/exports/${job.id}/download"]`);
  await expect(downloadLink).toBeVisible({ timeout: 120_000 });
  const downloadStarted = page.waitForEvent("download");
  await downloadLink.click();
  const download = await downloadStarted;
  expect(download.suggestedFilename()).toBe(`nvr-export-${job.id}.zip`);
  expect(await download.failure()).toBeNull();
});

test("touch-sized playback supports local timeline interaction without horizontal page overflow", async ({ page }) => {
  const viewport = { width: 390, height: 844 };
  const fixture = seedPlaybackFixtures(1);
  await page.setViewportSize(viewport);
  await signInToPlayback(page);
  await page.locator("#sidebar-cameras button").first().tap();
  await page.locator("#playback-start").fill(localDateTime(fixture.playback.start));
  await page.locator("#playback-end").fill(localDateTime(fixture.playback.end));
  await page.locator("#playback-search").tap();
  await expect(page.locator(".playback-camera video")).toHaveCount(1);
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewport);

  const initialTime = Number(await page.locator("#playback-cursor").getAttribute("data-time"));
  await page.locator("#playback-ruler").tap({ position: { x: 160, y: 12 } });
  await expect.poll(() => page.locator("#playback-cursor").getAttribute("data-time")).not.toBe(String(initialTime));
  await page.locator("#playback-play").tap();
  await expect.poll(() => page.locator(".playback-camera video").evaluate(element => element.paused)).toBe(false);
  await page.locator("#playback-pause").tap();
  await expect.poll(() => page.locator(".playback-camera video").evaluate(element => element.paused)).toBe(true);

  const touchScroll = await page.evaluate(() => document.documentElement.scrollHeight > innerHeight || document.querySelector("#console-layout").scrollHeight > document.querySelector("#console-layout").clientHeight);
  expect(touchScroll).toBe(true);
  await page.locator("#playback-export-open").tap();
  await expect(page.locator("#playback-export-dialog")).toBeVisible();
  await page.locator("#playback-export-close").tap();
});
