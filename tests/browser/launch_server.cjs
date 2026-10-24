const { spawn } = require("node:child_process");
const { loadFixtureEnvironment } = require("./fixture_environment.cjs");

const SERVER_ENVIRONMENT_KEYS = Object.freeze([
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
  "NVR_BACKUP_PASSPHRASE_FILE",
  "NVR_BACKUP_PATH",
]);

function childEnvironment(source = process.env) {
  return Object.fromEntries(
    SERVER_ENVIRONMENT_KEYS
      .filter(key => source[key] !== undefined)
      .map(key => [key, source[key]]),
  );
}

function run() {
  const server = spawn("python", ["tests/browser/server.py"], {
    cwd: process.cwd(),
    env: childEnvironment({ PATH: process.env.PATH, ...loadFixtureEnvironment() }),
    stdio: "inherit",
  });
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.kill(signal));
  }
  server.on("error", error => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
  server.on("exit", (code, signal) => {
    process.exitCode = signal ? 1 : (code ?? 1);
  });
}

if (require.main === module) run();

module.exports = { SERVER_ENVIRONMENT_KEYS, childEnvironment };
