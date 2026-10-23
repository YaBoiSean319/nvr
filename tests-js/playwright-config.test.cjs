const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "..");
const EXPECTED_SERVER_ENV_KEYS = [
  "NVR_BROWSER_FIXTURE_MANIFEST",
  "NVR_BROWSER_FIXTURE_PASSWORD_PATH",
  "NVR_BROWSER_HOST",
  "NVR_BROWSER_PORT",
  "NVR_COOKIE_SECURE",
  "NVR_CREDENTIAL_KEY_PATH",
  "NVR_DB_PATH",
  "NVR_DISCOVERY_INTERFACE",
  "NVR_RECORDINGS_PATH",
  "NVR_SEGMENT_SECONDS",
  "NVR_STORAGE_VOLUMES",
  "PATH",
].sort();

test("browser server and fallback reports do not expose unrelated environment values", () => {
  const sentinels = ["proxy-secret-sentinel", "credential-secret-sentinel", "runner-secret-sentinel"];
  const childProgram = [
    `const config = require(${JSON.stringify(path.join(ROOT, "playwright.config.cjs"))});`,
    'const fs = require("node:fs");',
    'const report = JSON.parse(fs.readFileSync("test-results/camera-browser-acceptance.json", "utf8"));',
    'const measurements = JSON.parse(fs.readFileSync("test-results/camera-admin-acceptance-measurements.json", "utf8"));',
    'process.stdout.write(JSON.stringify({ serverEnv: config.webServer.env, report, measurements }));',
  ].join("\n");
  const result = spawnSync(process.execPath, ["-e", childProgram], {
    cwd: ROOT,
    env: {
      ...process.env,
      HTTP_PROXY: sentinels[0],
      HTTPS_PROXY: sentinels[0],
      NVR_DB_PASSWORD: sentinels[1],
      AWS_SECRET_ACCESS_KEY: sentinels[2],
    },
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const output = JSON.parse(result.stdout);
  assert.deepEqual(Object.keys(output.serverEnv).sort(), EXPECTED_SERVER_ENV_KEYS);
  assert.equal(output.serverEnv.PATH, process.env.PATH);
  const serialized = JSON.stringify(output);
  for (const sentinel of sentinels) assert.equal(serialized.includes(sentinel), false);
  for (const name of ["HTTP_PROXY", "HTTPS_PROXY", "NVR_DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"]) {
    assert.equal(Object.hasOwn(output.serverEnv, name), false);
  }
  assert.equal(output.report.report_status, "not-collected");
  assert.equal(output.measurements.measurement_status, "not-collected");
});
