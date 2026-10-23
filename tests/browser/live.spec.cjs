// Real-browser journeys for the Live workspace (#91): Auto tiling, truthful
// tile state from decoded media, enlargement, audio and resource release.
const { spawn } = require("node:child_process");
const { test, expect, seedFixtures, signInNext, trackLiveSockets } = require("./helpers.cjs");

const AUTO_TABLE = { 0: null, 1: [1, 1], 2: [2, 1], 3: [2, 2], 4: [2, 2], 6: [3, 2], 9: [3, 3], 12: [4, 3], 16: [4, 4] };

function browserRow(page, name) {
  return page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
}

async function addCameras(page, count) {
  for (let index = 1; index <= count; index += 1) {
    await browserRow(page, `Camera ${String(index).padStart(3, "0")}`).click();
  }
}

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}:`) });
}

function publish(path, { audio = false } = {}) {
  const args = ["-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10"];
  if (audio) args.push("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000");
  args.push("-t", "300", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "10");
  args.push(...(audio ? ["-c:a", "aac", "-b:a", "32k"] : ["-an"]), "-f", "rtsp", "-rtsp_transport", "tcp", `rtsp://127.0.0.1:8554/${path}`);
  return spawn("ffmpeg", args, { stdio: "ignore" });
}

test("Auto layout follows the number of selected cameras and keeps video proportions", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(16);
  await signInNext(page);
  await expect(page.getByText("Choose cameras to start watching.")).toBeVisible();
  let selected = 0;
  for (const [count, expected] of Object.entries(AUTO_TABLE).map(([count, expected]) => [Number(count), expected])) {
    if (!count) continue;
    while (selected < count) {
      selected += 1;
      await browserRow(page, `Camera ${String(selected).padStart(3, "0")}`).click();
    }
    const grid = page.locator(".grid");
    await expect(grid).toHaveAttribute("data-columns", String(expected[0]));
    await expect(grid).toHaveAttribute("data-rows", String(expected[1]));
    const box = await page.locator(".grid-cell").first().boundingBox();
    expect(Math.abs(box.width / box.height - 16 / 9)).toBeLessThan(0.02);
  }
  await expect(page.getByText("16/16 in view")).toBeVisible();
  await browserRow(page, "Camera 001").click();
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "4");
  await expect(page.locator(".grid-cell")).toHaveCount(15);
});

test("adding and removing cameras never reconnects the others", async ({ page }) => {
  seedFixtures(4);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await addCameras(page, 2);
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  const firstId = await tile(page, "Camera 001").getAttribute("data-camera-id");
  await browserRow(page, "Camera 003").click();
  await browserRow(page, "Camera 004").click();
  await expect(tile(page, "Camera 004")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await browserRow(page, "Camera 002").click(); // remove
  await expect(tile(page, "Camera 002")).toHaveCount(0);
  await page.waitForTimeout(1_000);
  expect(sockets.opened(firstId)).toBe(1);
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "live");
});

test("a camera that stops shows stale, then reconnecting with its last frame, and recovers", async ({ page }) => {
  test.setTimeout(120_000);
  const path = `nvr-browser-flaky-${Date.now()}`;
  let publisher = publish(path);
  try {
    seedFixtures(2, { streams: { 1: `rtsp://127.0.0.1:8554/${path}` } });
    await signInNext(page);
    await new Promise(resolve => setTimeout(resolve, 2_000));
    await addCameras(page, 2);
    const flaky = tile(page, "Camera 001");
    await expect(flaky).toHaveAttribute("data-phase", "live", { timeout: 20_000 });
    publisher.kill("SIGKILL");
    await expect(flaky).toHaveAttribute("data-phase", /stale|reconnecting|offline/, { timeout: 20_000 });
    await expect(flaky.getByText(/Last frame \d/)).toBeVisible();
    const opacity = await flaky.locator("video").evaluate(element => Number(getComputedStyle(element).opacity));
    expect(opacity).toBeLessThan(1);
    await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "live");

    publisher = publish(path);
    await new Promise(resolve => setTimeout(resolve, 2_000));
    const retry = flaky.getByRole("button", { name: "Retry now" });
    if (await retry.isVisible()) await retry.click();
    await expect(flaky).toHaveAttribute("data-phase", "live", { timeout: 45_000 });
  } finally {
    publisher.kill("SIGKILL");
  }
});

test("enlarging suspends hidden tiles and restoring resumes each exactly once", async ({ page }) => {
  seedFixtures(3);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await addCameras(page, 3);
  for (const name of ["Camera 001", "Camera 002", "Camera 003"]) {
    await expect(tile(page, name)).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  }
  const ids = await page.locator(".tile").evaluateAll(tiles => tiles.map(element => element.dataset.cameraId));
  await tile(page, "Camera 002").dblclick();
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "1");
  await expect(tile(page, "Camera 001")).toBeHidden();
  await expect.poll(() => sockets.open(ids[0])).toBe(0);
  await expect.poll(() => sockets.open(ids[2])).toBe(0);
  await page.keyboard.press("Escape");
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "2");
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  expect(sockets.opened(ids[0])).toBe(2);
  expect(sockets.opened(ids[2])).toBe(2);
  // The enlarged camera switched to its main stream and back; nothing else churned.
  await expect(page.locator(".tile")).toHaveCount(3);
});

test("only one camera is audible and audio controls appear only where audio exists", async ({ page }) => {
  seedFixtures(3, { streams: { 1: "rtsp://127.0.0.1:8554/nvr-audio", 2: "rtsp://127.0.0.1:8554/nvr-audio" }, audio: [1, 2] });
  await signInNext(page);
  await addCameras(page, 3);
  for (const name of ["Camera 001", "Camera 002", "Camera 003"]) {
    await expect(tile(page, name)).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  }
  await expect(tile(page, "Camera 003").getByRole("button", { name: /Listen/ })).toHaveCount(0);
  const videos = page.locator(".tile video");
  await expect.poll(() => videos.evaluateAll(elements => elements.every(element => element.muted))).toBe(true);

  const first = tile(page, "Camera 001").getByRole("button", { name: "Listen to Camera 001" });
  await first.click();
  await expect(tile(page, "Camera 001").getByRole("button", { name: "Mute Camera 001" })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => tile(page, "Camera 001").locator("video").evaluate(element => element.muted)).toBe(false);

  await tile(page, "Camera 002").getByRole("button", { name: "Listen to Camera 002" }).click();
  await expect.poll(() => tile(page, "Camera 002").locator("video").evaluate(element => element.muted)).toBe(false);
  await expect.poll(() => tile(page, "Camera 001").locator("video").evaluate(element => element.muted)).toBe(true);
});

test("leaving Live releases every viewer, and browsing a large inventory starts no media", async ({ page }) => {
  seedFixtures(200);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await expect(page.getByRole("button", { name: /^Camera 200\b/ })).toBeVisible();
  await page.getByRole("searchbox", { name: /Search cameras/ }).fill("Camera 19");
  await expect(page.getByText(/10 matches/)).toBeVisible();
  expect(sockets.opened()).toBe(0);

  await page.getByRole("searchbox", { name: /Search cameras/ }).fill("");
  await addCameras(page, 2);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await expect.poll(() => sockets.open()).toBe(0);
});

test("tiles are fully usable from the keyboard", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await addCameras(page, 2);
  const first = tile(page, "Camera 001");
  await expect(first).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  await first.focus();
  await expect(first).toHaveAttribute("data-active", "true");
  await page.keyboard.press("Enter");
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "1");
  await page.keyboard.press("Escape");
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "2");
  await first.getByRole("button", { name: "Remove Camera 001 from view" }).focus();
  await page.keyboard.press("Enter");
  await expect(tile(page, "Camera 001")).toHaveCount(0);
  await expect(tile(page, "Camera 002")).toHaveAttribute("data-active", "true");
});
