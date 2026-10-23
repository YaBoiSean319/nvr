const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const rootDirectory = path.resolve(__dirname, "../..");
const runKey = crypto.createHash("sha256").update(rootDirectory).digest("hex").slice(0, 16);
const FIXTURE_ENVIRONMENT_PATH = path.join(os.tmpdir(), `nvr-camera-browser-env-${runKey}.json`);

function loadFixtureEnvironment() {
  return JSON.parse(fs.readFileSync(FIXTURE_ENVIRONMENT_PATH, "utf8"));
}

module.exports = { FIXTURE_ENVIRONMENT_PATH, loadFixtureEnvironment };
