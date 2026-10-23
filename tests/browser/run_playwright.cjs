const { spawnSync } = require("node:child_process");
const path = require("node:path");
const { cleanupFixtureEnvironment } = require("./fixture_environment.cjs");

let result;
try {
  const playwrightCli = path.join(path.dirname(require.resolve("playwright")), "cli.js");
  result = spawnSync(process.execPath, [playwrightCli, "test", ...process.argv.slice(2)], {
    cwd: process.cwd(),
    env: process.env,
    stdio: "inherit",
  });
} finally {
  cleanupFixtureEnvironment();
}

if (result.error) {
  process.stderr.write(`${result.error.message}\n`);
  process.exitCode = 1;
} else {
  process.exitCode = result.status ?? 1;
}
