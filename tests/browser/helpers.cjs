const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const { test: base, expect } = require("playwright/test");
const { loadFixtureEnvironment } = require("./fixture_environment.cjs");

const FIXTURE_ENVIRONMENT = loadFixtureEnvironment();
const FIXTURE_PASSWORD = fs.readFileSync(FIXTURE_ENVIRONMENT.NVR_BROWSER_FIXTURE_PASSWORD_PATH, "utf8").trim();
const MANIFEST_PATH = FIXTURE_ENVIRONMENT.NVR_BROWSER_FIXTURE_MANIFEST;

function seedFixtures(count, options = {}) {
  const args = ["-m", "tests.browser.seed_fixtures", "--count", String(count)];
  if (options.bulkTrigger) args.push("--bulk-trigger");
  if (options.failNameSave) args.push("--fail-name-save");
  if (options.extraGroup) args.push("--extra-group");
  if (options.freshInstall) args.push("--fresh-install");
  if (options.footage) args.push("--footage");
  if (options.longNames) args.push("--long-names");
  for (const [index, url] of Object.entries(options.streams ?? {})) args.push("--stream", `${index}=${url}`);
  for (const index of options.audio ?? []) args.push("--audio", String(index));
  const result = spawnSync("python", args, {
    cwd: process.cwd(),
    env: { PATH: process.env.PATH, ...FIXTURE_ENVIRONMENT },
    encoding: "utf8",
    timeout: 30_000,
  });
  assert.equal(result.status, 0, `fixture seed failed: ${result.stderr || result.stdout}`);
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
}

async function signIn(page, username = "camera-admin") {
  const pageErrors = [];
  const onPageError = error => pageErrors.push(error.message);
  page.on("pageerror", onPageError);
  try {
    await page.goto("/");
    try {
      await page.locator("#login").waitFor({ state: "visible", timeout: 8_000 });
    } catch {
      const setupResponse = await page.request.get(new URL("/api/setup/status", page.url()).toString()).catch(() => null);
      const setupStatus = setupResponse?.ok() ? await setupResponse.json().catch(() => null) : null;
      const setupRequired = setupStatus ? String(setupStatus.setup_required) : "unknown";
      throw new Error(`Login screen is hidden. Initial setup required: ${setupRequired}. Page errors: ${pageErrors.join("; ") || "(none)"}`);
    }
    await expect(page.locator("#login")).toBeVisible();
    await page.locator("#login-user").fill(username);
    await page.locator("#login-pass").fill(FIXTURE_PASSWORD);
    const loginResponsePromise = page.waitForResponse(response => {
      try { return new URL(response.url()).pathname === "/api/auth/login"; }
      catch { return false; }
    }, { timeout: 8_000 }).catch(() => null);
    await page.getByRole("button", { name: "Sign in" }).click();
    const loginResponse = await loginResponsePromise;
    const message = (await page.locator("#message").innerText().catch(() => "")).trim();
    if (!loginResponse) {
      throw new Error(`No /api/auth/login response. Message: ${message || "(empty)"}. Page errors: ${pageErrors.join("; ") || "(none)"}`);
    }
    if (!loginResponse.ok()) {
      const setupResponse = loginResponse.status() === 401
        ? await page.request.get(new URL("/api/setup/status", page.url()).toString()).catch(() => null)
        : null;
      const setupStatus = setupResponse?.ok()
        ? await setupResponse.json().catch(() => null)
        : null;
      const accountConfigured = setupStatus
        ? String(setupStatus.setup_required === false)
        : "unknown";
      throw new Error(`Fixture login returned HTTP ${loginResponse.status()}. Message: ${message || "(empty)"}. Fixture account configured: ${accountConfigured}.`);
    }
    await expect(page.locator("#status"), `Login returned HTTP ${loginResponse.status()}, but the workspace stayed hidden. Message: ${message || "(empty)"}. Page errors: ${pageErrors.join("; ") || "(none)"}`).toBeVisible();
    await expect(page.locator('#console-nav [data-screen="camera-settings"]')).toBeVisible();
  } finally {
    page.off("pageerror", onPageError);
  }
}

async function openCameraSettings(page) {
  await page.locator('#console-nav [data-screen="camera-settings"]').click();
  await expect(page.locator("#camera-inventory-table")).toBeVisible();
  const fixture = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "ready");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(fixture.camera_count);
}

async function selectCamera(page, cameraId) {
  await page.locator(`#camera-inventory-table button[data-camera-id="${cameraId}"]`).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${cameraId}"]`)).toBeVisible();
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function percentile(values, percent) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil((percent / 100) * sorted.length) - 1];
}

function writeAcceptanceMeasurements(record) {
  const output = "test-results/camera-admin-acceptance-measurements.json";
  fs.mkdirSync("test-results", { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(record, null, 2)}\n`, { mode: 0o600 });
  return output;
}

/** Sign in to the rebuilt interface at /next/. */
async function signInNext(page, username = "camera-admin") {
  await page.goto("/next/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(FIXTURE_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
}

/** Record every live WebSocket the page opens, by camera. */
function trackLiveSockets(page) {
  const sockets = [];
  page.on("websocket", socket => {
    const match = /\/api\/cameras\/([^/]+)\/live\/stream/.exec(socket.url());
    if (!match) return;
    const entry = { cameraId: decodeURIComponent(match[1]), url: socket.url(), closed: false };
    socket.on("close", () => { entry.closed = true; });
    sockets.push(entry);
  });
  return {
    opened: cameraId => sockets.filter(entry => !cameraId || entry.cameraId === cameraId).length,
    open: cameraId => sockets.filter(entry => !entry.closed && (!cameraId || entry.cameraId === cameraId)).length,
  };
}

module.exports = { test: base, expect, FIXTURE_PASSWORD, seedFixtures, signIn, signInNext, trackLiveSockets, openCameraSettings, selectCamera, median, percentile, writeAcceptanceMeasurements };
