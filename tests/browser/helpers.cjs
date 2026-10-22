const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const { test: base, expect } = require("playwright/test");

const FIXTURE_PASSWORD = "BrowserFixturePassphrase2026";
const MANIFEST_PATH = process.env.NVR_BROWSER_FIXTURE_MANIFEST;

function seedFixtures(count, options = {}) {
  const args = ["-m", "tests.browser.seed_fixtures", "--count", String(count)];
  if (options.bulkTrigger) args.push("--bulk-trigger");
  if (options.failNameSave) args.push("--fail-name-save");
  const result = spawnSync("python", args, {
    cwd: process.cwd(),
    env: process.env,
    encoding: "utf8",
    timeout: 30_000,
  });
  assert.equal(result.status, 0, `fixture seed failed: ${result.stderr || result.stdout}`);
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
}

async function signIn(page, username = "camera-admin") {
  await page.goto("/");
  await expect(page.locator("#login")).toBeVisible();
  await page.locator("#login-user").fill(username);
  await page.locator("#login-pass").fill(FIXTURE_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("#status")).toBeVisible();
  await expect(page.locator('#console-nav [data-screen="camera-settings"]')).toBeVisible();
}

async function openCameraSettings(page) {
  await page.locator('#console-nav [data-screen="camera-settings"]').click();
  await expect(page.locator("#camera-inventory-table")).toBeVisible();
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
