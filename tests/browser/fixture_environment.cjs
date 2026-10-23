const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const rootDirectory = path.resolve(__dirname, "../..");
const runKey = crypto.createHash("sha256").update(rootDirectory).digest("hex").slice(0, 16);
const FIXTURE_DIRECTORY = path.join(os.tmpdir(), `nvr-camera-browser-${runKey}`);
const FIXTURE_ENVIRONMENT_PATH = path.join(os.tmpdir(), `nvr-camera-browser-env-${runKey}.json`);
const FIXTURE_PASSWORD_PATH = path.join(FIXTURE_DIRECTORY, "browser-fixture-password");

function loadFixtureEnvironment() {
  return JSON.parse(fs.readFileSync(FIXTURE_ENVIRONMENT_PATH, "utf8"));
}

function cleanupFixtureEnvironment() {
  fs.rmSync(FIXTURE_DIRECTORY, { recursive: true, force: true });
  fs.rmSync(FIXTURE_ENVIRONMENT_PATH, { force: true });
}

module.exports = {
  FIXTURE_DIRECTORY,
  FIXTURE_ENVIRONMENT_PATH,
  FIXTURE_PASSWORD_PATH,
  cleanupFixtureEnvironment,
  loadFixtureEnvironment,
};
