const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
const test = require("node:test");
const { childEnvironment } = require("../tests/browser/launch_server.cjs");
const { cleanupFixtureEnvironment } = require("../tests/browser/fixture_environment.cjs");

const ROOT = path.resolve(__dirname, "..");
const EXPECTED_SERVER_ENV_KEYS = [
  "NVR_BACKUP_PASSPHRASE_FILE",
  "NVR_BACKUP_PATH",
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

test("browser server, fallback reports, and failure diagnostics protect credentials", () => {
  const sentinels = ["proxy-secret-sentinel", "credential-secret-sentinel", "runner-secret-sentinel"];
  const configPath = path.join(ROOT, "playwright.config.cjs");
  const fixturePath = path.join(ROOT, "tests/browser/fixture_environment.cjs");
  const isolatedWorkerProgram = [
    `require(${JSON.stringify(configPath)});`,
    `const fixture = require(${JSON.stringify(fixturePath)});`,
    'const environment = fixture.loadFixtureEnvironment();',
    'process.stdout.write(JSON.stringify({ keys: Object.keys(environment).sort(), dbPath: environment.NVR_DB_PATH, passwordPath: environment.NVR_BROWSER_FIXTURE_PASSWORD_PATH }));',
  ].join(" ");
  const childProgram = [
    `const ROOT = ${JSON.stringify(ROOT)};`,
    `const config = require(${JSON.stringify(configPath)});`,
    'const fs = require("node:fs");',
    'const { spawnSync } = require("node:child_process");',
    `const fixture = require(${JSON.stringify(fixturePath)});`,
    'const report = JSON.parse(fs.readFileSync("test-results/camera-browser-acceptance.json", "utf8"));',
    'const measurements = JSON.parse(fs.readFileSync("test-results/camera-admin-acceptance-measurements.json", "utf8"));',
    'const fixtureEnvironment = fixture.loadFixtureEnvironment();',
    'const fixtureEnvironmentMode = fs.statSync(fixture.FIXTURE_ENVIRONMENT_PATH).mode & 0o777;',
    'const fixturePassword = fs.readFileSync(fixtureEnvironment.NVR_BROWSER_FIXTURE_PASSWORD_PATH, "utf8").trim();',
    `const workerProgram = ${JSON.stringify(isolatedWorkerProgram)};`,
    'const isolatedWorker = spawnSync(process.execPath, ["-e", workerProgram], { cwd: ROOT, env: { PATH: process.env.PATH }, encoding: "utf8" });',
    'if (isolatedWorker.status !== 0) throw new Error(isolatedWorker.stderr || "isolated fixture worker failed");',
    'const isolatedWorkerEnvironment = JSON.parse(isolatedWorker.stdout);',
    'const fixtureEnvironmentLeaksPassword = JSON.stringify(fixtureEnvironment).includes(fixturePassword);',
    'const reportLeaksPassword = JSON.stringify(report).includes(fixturePassword);',
    'process.stdout.write(JSON.stringify({ serverEnv: config.webServer.env, trace: config.use.trace, captureGitInfo: config.captureGitInfo, fixtureEnvironment, fixtureEnvironmentMode, isolatedWorkerEnvironment, fixtureEnvironmentLeaksPassword, reportLeaksPassword, report, measurements }));',
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
  assert.deepEqual(Object.keys(output.fixtureEnvironment).sort(), EXPECTED_SERVER_ENV_KEYS.filter(key => key !== "PATH"));
  assert.deepEqual(output.isolatedWorkerEnvironment.keys, Object.keys(output.fixtureEnvironment).sort());
  assert.equal(output.isolatedWorkerEnvironment.dbPath, output.fixtureEnvironment.NVR_DB_PATH);
  assert.equal(output.isolatedWorkerEnvironment.passwordPath, output.fixtureEnvironment.NVR_BROWSER_FIXTURE_PASSWORD_PATH);
  assert.equal(output.fixtureEnvironmentMode, 0o600);
  assert.equal(output.fixtureEnvironmentLeaksPassword, false);
  assert.equal(output.reportLeaksPassword, false);
  assert.equal(output.trace, "off", "Playwright traces can contain authenticated request bodies");
  assert.deepEqual(output.captureGitInfo, { diff: false }, "CI reports must not embed the full source diff");
  assert.equal(output.serverEnv.PATH, process.env.PATH);
  const serialized = JSON.stringify(output);
  for (const sentinel of sentinels) assert.equal(serialized.includes(sentinel), false);
  for (const name of ["HTTP_PROXY", "HTTPS_PROXY", "NVR_DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"]) {
    assert.equal(Object.hasOwn(output.serverEnv, name), false);
  }
  const childEnv = childEnvironment({
    ...output.serverEnv,
    HTTP_PROXY: sentinels[0],
    NVR_DB_PASSWORD: sentinels[1],
    AWS_SECRET_ACCESS_KEY: sentinels[2],
    NVR_BROWSER_REPORT_PRIVACY_SENTINEL: "report-sentinel",
  });
  assert.deepEqual(Object.keys(childEnv).sort(), EXPECTED_SERVER_ENV_KEYS);
  const serializedChildEnv = JSON.stringify(childEnv);
  for (const sentinel of [...sentinels, "report-sentinel"]) assert.equal(serializedChildEnv.includes(sentinel), false);
  assert.equal(output.report.report_status, "not-collected");
  assert.equal(output.measurements.measurement_status, "not-collected");
});

test.after(() => cleanupFixtureEnvironment());
