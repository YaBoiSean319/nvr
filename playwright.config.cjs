const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  FIXTURE_DIRECTORY,
  FIXTURE_ENVIRONMENT_PATH,
  FIXTURE_PASSWORD_PATH,
} = require("./tests/browser/fixture_environment.cjs");

fs.mkdirSync(FIXTURE_DIRECTORY, { recursive: true, mode: 0o700 });
fs.chmodSync(FIXTURE_DIRECTORY, 0o700);
try {
  fs.writeFileSync(FIXTURE_PASSWORD_PATH, require("node:crypto").randomBytes(32).toString("base64url"), { flag: "wx", mode: 0o600 });
} catch (error) {
  if (error.code !== "EEXIST") throw error;
}
fs.chmodSync(FIXTURE_PASSWORD_PATH, 0o600);

const artifactDirectory = path.join(process.cwd(), "test-results");
fs.mkdirSync(artifactDirectory, { recursive: true });
const measuredEvidencePath = path.join(artifactDirectory, "camera-admin-acceptance-measurements.json");
const testReportPath = path.join(artifactDirectory, "camera-browser-acceptance.json");
fs.writeFileSync(measuredEvidencePath, `${JSON.stringify({
  record_type: "camera-admin-acceptance-measurements",
  measurement_status: "not-collected",
  reason: "The 200-camera browser measurement journey did not complete.",
  app_version: null,
  browser: "Chromium",
  browser_version: null,
  environment: null,
  configured_camera_count: 200,
  sample_counts: { filter_feedback: 0, panel_feedback: 0 },
  error_counts: null,
  filter_feedback_ms: { p50: null, p95: null, samples: [] },
  panel_feedback_ms: { p50: null, p95: null, samples: [] },
  inventory_live_requests_before_explicit_preview: null,
  production_capacity_gate: "unchecked",
  thirty_day_qualification_gate: "unchecked",
}, null, 2)}\n`, { mode: 0o600 });
fs.writeFileSync(testReportPath, `${JSON.stringify({
  report_status: "not-collected",
  reason: "Playwright did not complete its JSON reporter.",
  tests: [],
  stats: { expected: 0, unexpected: 0, skipped: 0, flaky: 0 },
}, null, 2)}\n`, { mode: 0o600 });

const fixtureEnvironment = {
  NVR_DB_PATH: path.join(FIXTURE_DIRECTORY, "state.sqlite3"),
  NVR_RECORDINGS_PATH: path.join(FIXTURE_DIRECTORY, "recordings"),
  NVR_CREDENTIAL_KEY_PATH: path.join(FIXTURE_DIRECTORY, "credentials.key"),
  NVR_COOKIE_SECURE: "false",
  NVR_STORAGE_VOLUMES: JSON.stringify({ default: "", archive: "archive" }),
  NVR_BROWSER_FIXTURE_MANIFEST: path.join(FIXTURE_DIRECTORY, "fixtures.json"),
  NVR_BROWSER_FIXTURE_PASSWORD_PATH: FIXTURE_PASSWORD_PATH,
  NVR_BROWSER_HOST: "127.0.0.1",
  NVR_BROWSER_PORT: "8765",
  NVR_SEGMENT_SECONDS: "10",
  NVR_DISCOVERY_INTERFACE: "127.0.0.1",
};
fs.writeFileSync(FIXTURE_ENVIRONMENT_PATH, `${JSON.stringify(fixtureEnvironment, null, 2)}\n`, { mode: 0o600 });
fs.chmodSync(FIXTURE_ENVIRONMENT_PATH, 0o600);
// The server only needs PATH and these isolated fixture settings. Playwright
// merges process.env into webServer children, so launch_server.cjs enforces this
// allowlist at the actual Python server boundary.
const serverEnvironment = {
  PATH: process.env.PATH || "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
  ...fixtureEnvironment,
};

module.exports = {
  testDir: "./tests/browser",
  testMatch: "**/*.spec.cjs",
  // CI reports should retain the tested commit but must not embed the full PR
  // source diff, which can include workflow-only privacy sentinels.
  captureGitInfo: { diff: false },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: testReportPath }],
  ],
  outputDir: "test-results/playwright",
  use: {
    baseURL: "http://127.0.0.1:8765",
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    hasTouch: true,
    // Traces include authenticated network request bodies; do not retain them
    // with CI artifacts because browser journeys sign in using fixture credentials.
    trace: "off",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 8_000,
    navigationTimeout: 10_000,
  },
  webServer: {
    command: "node tests/browser/launch_server.cjs",
    url: "http://127.0.0.1:8765/healthz",
    timeout: 60_000,
    reuseExistingServer: false,
    env: serverEnvironment,
    stdout: "pipe",
    stderr: "pipe",
  },
};
