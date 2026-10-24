// Installable web app (#110): manifest from branding, and a service worker that keeps only the
// application shell and hashed assets.
const { chromium } = require("playwright");
const { test, expect, seedFixtures, signInNext, FIXTURE_PASSWORD } = require("./helpers.cjs");

/** Wait until the service worker controls the page (the first load installs it). */
async function controlled(page) {
  await page.evaluate(() => navigator.serviceWorker.ready);
  if (!(await page.evaluate(() => Boolean(navigator.serviceWorker.controller)))) await page.reload();
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);
}

async function cachedPaths(page) {
  return page.evaluate(async () => {
    const paths = [];
    for (const name of await caches.keys()) {
      for (const request of await (await caches.open(name)).keys()) paths.push(`${name} ${new URL(request.url).pathname}`);
    }
    return paths;
  });
}

test("Chromium reports the interface installable, named and iconed from branding", async ({ page, baseURL }, testInfo) => {
  seedFixtures(1);
  // Chromium never offers to install from an incognito context, which Playwright's default
  // contexts are; a real profile shows what an operator's browser reports.
  const profile = await chromium.launchPersistentContext(testInfo.outputPath("profile"), {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined, baseURL,
  });
  try {
    const installing = profile.pages()[0] ?? await profile.newPage();
    await installing.goto("/");
    const session = await profile.newCDPSession(installing);
    await expect.poll(async () => (await session.send("Page.getInstallabilityErrors")).installabilityErrors, { timeout: 15_000 }).toEqual([]);
  } finally {
    await profile.close();
  }
  await page.goto("/");
  const manifest = await (await page.request.get(await page.locator("link[rel=manifest]").getAttribute("href"))).json();
  expect(manifest).toMatchObject({ name: "Workplace NVR", display: "standalone", start_url: "/" });

  // Changing branding changes what the installed app shows after its next load.
  await signInNext(page);
  expect((await page.request.put("/api/settings/branding", { data: { app_name: "Harbour Security", primary_color: "#0f3d2e", accent_color: "#f6f064" } })).ok()).toBeTruthy();
  await page.reload();
  const href = await page.locator("link[rel=manifest]").getAttribute("href");
  const updated = await (await page.request.get(href)).json();
  expect(updated).toMatchObject({ name: "Harbour Security", theme_color: "#0f3d2e" });
  expect(updated.icons.map(icon => icon.src)).not.toEqual(manifest.icons.map(icon => icon.src));
  await expect(page.locator("meta[name=theme-color]")).toHaveAttribute("content", "#0f3d2e");
});

test("the service worker never stores API, media or snapshot responses", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page);
  await controlled(page);
  // Exercise API reads, recorded media and a snapshot while the worker is in control.
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i }).click();
  await page.getByRole("dialog", { name: "Alarms" }).getByRole("button", { name: /^Gate entry/ }).click();
  await expect.poll(() => page.getByRole("dialog", { name: "Alarms" }).getByRole("img").evaluate(image => image.complete && image.naturalWidth)).toBeGreaterThan(0);
  await expect.poll(() => page.getByLabel("Alarm clip").evaluate(video => video.readyState)).toBeGreaterThanOrEqual(1);

  const paths = await cachedPaths(page);
  expect(paths.length).toBeGreaterThan(0);
  for (const entry of paths) expect(entry, "only the shell and hashed assets are cached").toMatch(/^nvr-shell-[0-9a-f]{16} (\/|\/assets\/\S+)$/);
});

test("a new release replaces a cached shell instead of showing a stale interface", async ({ page }) => {
  seedFixtures(1);
  // An earlier release's cache, as an installed app would still have it.
  await page.goto("/healthz");
  await page.evaluate(async () => {
    const cache = await caches.open("nvr-shell-0000000000000000");
    await cache.put("/", new Response("<!doctype html><title>Stale release</title><p>stale</p>", { headers: { "Content-Type": "text/html" } }));
  });
  await page.goto("/");
  await controlled(page);
  await expect(page.getByLabel("Username")).toBeVisible();
  await expect(page).not.toHaveTitle("Stale release");
  await expect.poll(async () => (await page.evaluate(() => caches.keys())).filter(name => name.startsWith("nvr-shell-"))).not.toContain("nvr-shell-0000000000000000");
  const current = (await page.evaluate(() => caches.keys())).filter(name => name.startsWith("nvr-shell-"));
  expect(current).toHaveLength(1);
  expect((await page.request.get("/sw.js")).ok()).toBeTruthy();
  expect(await page.request.get("/sw.js").then(response => response.text())).toContain(current[0].replace("nvr-shell-", ""));
});

test("signing out leaves no credentials, session or camera data in browser storage", async ({ page }) => {
  seedFixtures(2, { footage: true });
  await signInNext(page);
  await controlled(page);
  const cookie = (await page.context().cookies()).find(item => item.name === "nvr_session");
  expect(cookie).toBeTruthy();
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: /camera-admin/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByLabel("Username")).toBeVisible();

  expect((await page.context().cookies()).find(item => item.name === "nvr_session")).toBeUndefined();
  const stored = await page.evaluate(async () => {
    const values = [];
    for (const storage of [localStorage, sessionStorage]) for (let index = 0; index < storage.length; index += 1) values.push(storage.getItem(storage.key(index)));
    for (const name of await caches.keys()) {
      for (const request of await (await caches.open(name)).keys()) {
        values.push(request.url);
        // The shipped scripts and styles are public code; only the cached page could hold data.
        if (!new URL(request.url).pathname.startsWith("/assets/")) values.push(await (await (await caches.open(name)).match(request)).text());
      }
    }
    return values.join("\n");
  });
  expect(stored).not.toContain(cookie.value);
  expect(stored).not.toContain(FIXTURE_PASSWORD);
  expect(stored).not.toContain("rtsp://");
  expect(stored).not.toContain("/api/");
});
