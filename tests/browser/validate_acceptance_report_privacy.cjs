const fs = require("node:fs");
const path = require("node:path");

const reportPath = path.resolve("test-results/camera-browser-acceptance.json");
const allowedWebServerEnvironment = new Set([
  "PATH",
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
]);

if (!fs.existsSync(reportPath)) throw new Error("Playwright JSON report is missing; privacy validation cannot run.");
const reportText = fs.readFileSync(reportPath, "utf8");
const report = JSON.parse(reportText);
const sentinel = process.env.NVR_BROWSER_REPORT_PRIVACY_SENTINEL;
if (sentinel && reportText.includes(sentinel)) {
  throw new Error("Playwright JSON report contains the browser-runner privacy sentinel.");
}

let checkedServerEnvironment = false;
function inspect(value, parentPath = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => inspect(item, [...parentPath, String(index)]));
    return;
  }
  if (!value || typeof value !== "object") return;
  if (parentPath.at(-1) === "webServer" && Object.hasOwn(value, "env")) {
    if (!value.env || typeof value.env !== "object" || Array.isArray(value.env)) {
      throw new Error("Playwright JSON report has an invalid webServer.env value.");
    }
    const unexpected = Object.keys(value.env).filter(key => !allowedWebServerEnvironment.has(key));
    if (unexpected.length) throw new Error(`Playwright JSON report contains unexpected webServer.env keys: ${unexpected.join(", ")}`);
    checkedServerEnvironment = true;
  }
  for (const [key, child] of Object.entries(value)) inspect(child, [...parentPath, key]);
}
inspect(report);
process.stdout.write(`Playwright JSON privacy validation passed${checkedServerEnvironment ? " (webServer.env checked)" : " (no webServer.env serialized)"}.\n`);
