const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const { test: base, expect } = require("playwright/test");

const FIXTURE_PASSWORD = fs.readFileSync(process.env.NVR_BROWSER_FIXTURE_PASSWORD_PATH, "utf8").trim();
const MANIFEST_PATH = process.env.NVR_BROWSER_FIXTURE_MANIFEST;
const FIXTURE_ENVIRONMENT_KEYS = [
  "NVR_DB_PATH",
  "NVR_RECORDINGS_PATH",
  "NVR_CREDENTIAL_KEY_PATH",
  "NVR_COOKIE_SECURE",
  "NVR_STORAGE_VOLUMES",
  "NVR_BROWSER_FIXTURE_MANIFEST",
  "NVR_BROWSER_FIXTURE_PASSWORD_PATH",
  "NVR_BROWSER_HOST",
  "NVR_BROWSER_PORT",
  "NVR_SEGMENT_SECONDS",
  "NVR_DISCOVERY_INTERFACE",
];

function seedFixtures(count, options = {}) {
  const args = ["-m", "tests.browser.seed_fixtures", "--count", String(count)];
  if (options.bulkTrigger) args.push("--bulk-trigger");
  if (options.failNameSave) args.push("--fail-name-save");
  if (options.extraGroup) args.push("--extra-group");
  const result = spawnSync("python", args, {
    cwd: process.cwd(),
    env: Object.fromEntries(["PATH", ...FIXTURE_ENVIRONMENT_KEYS].map(key => [key, process.env[key]]).filter(([, value]) => value !== undefined)),
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
      throw new Error(`Fixture login returned HTTP ${loginResponse.status()}. Message: ${message || "(empty)"}.`);
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

module.exports = { test: base, expect, FIXTURE_PASSWORD, seedFixtures, signIn, openCameraSettings, selectCamera, median, percentile, writeAcceptanceMeasurements };
