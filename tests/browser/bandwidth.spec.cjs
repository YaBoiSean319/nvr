// Bandwidth-aware viewing (#134): Auto quality steps down to the substream when video arrives late and back up when it
// recovers, never more often than the configured interval, never over a manual choice; low-data mode holds tiles to
// substreams, caps them and lets go of tiles nobody sees.
const { test, expect, seedFixtures, signInNext, trackLiveSockets } = require("./helpers.cjs");

const STREAM = "rtsp://127.0.0.1:8554/nvr-test";
const INTERVAL_SECONDS = 4;

function tile(page, name) {
  return page.getByRole("group", { name: new RegExp(`^${name}:`) });
}

async function addCameras(page, count) {
  for (let index = 1; index <= count; index += 1) {
    await page.getByRole("button", { name: new RegExp(`^Camera ${String(index).padStart(3, "0")}\\b`) }).click();
  }
}

function viewing(page, prefs) {
  return page.addInitScript(value => window.localStorage.setItem("nvr.viewing", JSON.stringify(value)), prefs);
}

/** Sits between the page and the NVR's live sockets: when slow, video comes in bursts every few seconds instead of steadily. */
async function throttle(page) {
  const control = { slow: false, connections: [] };
  await page.routeWebSocket(/\/live\/stream/, socket => {
    const server = socket.connectToServer();
    const url = new URL(socket.url());
    control.connections.push({ quality: url.searchParams.get("quality"), at: Date.now() });
    let held = [];
    const flush = setInterval(() => {
      if (!control.slow) return;
      for (const message of held.splice(0)) socket.send(message);
    }, 3_000);
    server.onMessage(message => {
      if (typeof message === "string" || !control.slow) {
        for (const earlier of held.splice(0)) socket.send(earlier);
        socket.send(message);
      } else {
        held.push(message);
      }
    });
    socket.onMessage(message => server.send(message));
    socket.onClose(() => clearInterval(flush));
  });
  return control;
}

test("Auto steps down to the substream on a slow connection and back up when it recovers, no more often than the interval", async ({ page }) => {
  test.setTimeout(180_000);
  seedFixtures(2, { substreams: { 1: STREAM, 2: STREAM } });
  await viewing(page, { lowData: "off", intervalSeconds: INTERVAL_SECONDS });
  const network = await throttle(page);
  await signInNext(page);
  await addCameras(page, 2);
  const big = tile(page, "Camera 001");
  await expect(big).toHaveAttribute("data-phase", "live", { timeout: 20_000 });
  await big.dblclick();
  await expect(big).toHaveAttribute("data-quality", "main");
  await expect(big).toHaveAttribute("data-phase", "live", { timeout: 20_000 });
  await expect(big.getByTestId("tile-quality-note")).toHaveCount(0);

  network.slow = true;
  await expect(big).toHaveAttribute("data-quality", "sub", { timeout: 60_000 });
  await expect(big.getByTestId("tile-quality-note")).toContainText(/slow.*using Sub/i);
  await expect(big).toHaveAttribute("data-phase", "live", { timeout: 20_000 });

  network.slow = false;
  await expect(big).toHaveAttribute("data-quality", "main", { timeout: 90_000 });
  await expect(big.getByTestId("tile-quality-note")).toContainText(/recovered.*back on Main/i);

  // The last three connections are the enlarged main stream, the step down and the step up, each at least an interval after the one before.
  const last = network.connections.slice(-3);
  expect(last.map(connection => connection.quality)).toEqual(["high", "low", "high"]);
  for (let index = 1; index < last.length; index += 1) {
    expect(last[index].at - last[index - 1].at).toBeGreaterThanOrEqual(INTERVAL_SECONDS * 1_000 - 500);
  }
});

test("a Main or Sub choice is never overridden by Auto", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(1, { substreams: { 1: STREAM } });
  await viewing(page, { lowData: "off", intervalSeconds: INTERVAL_SECONDS });
  const network = await throttle(page);
  await signInNext(page);
  await addCameras(page, 1);
  const only = tile(page, "Camera 001");
  await expect(only).toHaveAttribute("data-phase", "live", { timeout: 20_000 });
  await page.getByLabel("Video quality for Camera 001").selectOption("main");
  await expect(only).toHaveAttribute("data-quality", "main");
  await expect(only).toHaveAttribute("data-quality-mode", "manual");
  await expect(only).toHaveAttribute("data-phase", "live", { timeout: 20_000 });
  network.slow = true;
  await page.waitForTimeout(INTERVAL_SECONDS * 1_000 * 3 + 6_000);
  await expect(only).toHaveAttribute("data-quality", "main");
  expect(network.connections.filter(connection => connection.quality === "low")).toHaveLength(0);
  await expect(only.getByTestId("tile-quality-note")).toHaveCount(0);
});

test("low-data mode uses substreams, caps the tiles that play, and releases tiles nobody can see", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(6, { substreams: Object.fromEntries([1, 2, 3, 4, 5, 6].map(index => [index, STREAM])) });
  await viewing(page, { lowData: "on", intervalSeconds: 30 });
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await addCameras(page, 6);
  for (const name of ["Camera 001", "Camera 002", "Camera 003", "Camera 004"]) {
    await expect(tile(page, name)).toHaveAttribute("data-phase", "live", { timeout: 25_000 });
    await expect(tile(page, name)).toHaveAttribute("data-quality", "sub");
    await expect(tile(page, name)).toHaveAttribute("data-quality-mode", "low-data");
  }
  const fifth = tile(page, "Camera 005");
  await expect(fifth).toHaveAttribute("data-phase", "paused");
  await expect(fifth.getByTestId("tile-quality-note")).toContainText("only 4 tiles play");
  await expect.poll(() => sockets.open()).toBe(4);
  await expect(page.getByRole("button", { name: "Data use" })).toHaveText("Data: Low");

  // Tapping a held tile plays it.
  await fifth.getByRole("button", { name: "Play this tile" }).click();
  await expect(fifth).toHaveAttribute("data-phase", "live", { timeout: 25_000 });
  await expect.poll(() => sockets.open()).toBe(5);

  // A page nobody is looking at releases every slot, and watching again brings the tiles back.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => sockets.open()).toBe(0);
  await expect(tile(page, "Camera 001").getByTestId("tile-quality-note")).toContainText("paused while out of sight");
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-phase", "live", { timeout: 25_000 });
  await expect.poll(() => sockets.open()).toBe(5);
});

test("low data turns on by itself for the installed app on a cellular connection, and can be switched from the menu", async ({ page }) => {
  seedFixtures(1, { substreams: { 1: STREAM } });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "connection", { configurable: true, value: { type: "cellular", saveData: false } });
    const original = window.matchMedia.bind(window);
    window.matchMedia = query => (query.includes("display-mode: standalone") ? { ...original("(min-width: 0px)"), matches: true, media: query } : original(query));
  });
  await signInNext(page);
  await addCameras(page, 1);
  await expect(page.getByRole("button", { name: "Data use" })).toHaveText("Data: Low");
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-quality-mode", "low-data");
  await page.getByRole("button", { name: "Data use" }).click();
  await page.getByRole("menuitemradio", { name: "Normal" }).click();
  await expect(page.getByRole("button", { name: "Data use" })).toHaveText("Data: Normal");
  await expect(tile(page, "Camera 001")).toHaveAttribute("data-quality-mode", "auto");
});
