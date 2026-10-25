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
  if (options.detections) args.push("--detections");
  if (options.alarm) args.push("--alarm");
  for (const [index, url] of Object.entries(options.streams ?? {})) args.push("--stream", `${index}=${url}`);
  for (const index of options.audio ?? []) args.push("--audio", String(index));
  for (const [index, url] of Object.entries(options.devices ?? {})) args.push("--device", `${index}=${url}`);
  const result = spawnSync("python", args, {
    cwd: process.cwd(),
    env: { PATH: process.env.PATH, ...FIXTURE_ENVIRONMENT },
    encoding: "utf8",
    timeout: 30_000,
  });
  assert.equal(result.status, 0, `fixture seed failed: ${result.stderr || result.stdout}`);
  return JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
}

/** Raise a new alarm on fixture camera `camera` from a rule that pops into Live, with those neighbours. */
function raiseAlarm(camera, { neighbours = [], rule = "Gate entry" } = {}) {
  const args = ["-m", "tests.browser.raise_alarm", "--camera", String(camera), "--neighbours", neighbours.join(","), "--rule", rule];
  const result = spawnSync("python", args, { cwd: process.cwd(), env: { PATH: process.env.PATH, ...FIXTURE_ENVIRONMENT }, encoding: "utf8", timeout: 30_000 });
  assert.equal(result.status, 0, `raising an alarm failed: ${result.stderr || result.stdout}`);
  return JSON.parse(result.stdout);
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

/** Sign in to the web interface. */
async function signInNext(page, username = "camera-admin") {
  await page.goto("/");
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

module.exports = { test: base, expect, FIXTURE_PASSWORD, seedFixtures, raiseAlarm, signInNext, trackLiveSockets, median, percentile, writeAcceptanceMeasurements };
