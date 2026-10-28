// Real-browser journeys for the multi-site console (#141): a second NVR is started as a site, federated into the NVR under test, and its cameras, alarms and live video
// are used through the console. The site listens on this machine's private network address (the console does not call loopback addresses).
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, expect, signInNext, seedFixtures } = require("./helpers.cjs");

const ROOT = path.resolve(__dirname, "../..");
const PORT = 8791;

function privateAddress() {
  for (const addresses of Object.values(os.networkInterfaces())) {
    for (const item of addresses ?? []) {
      if (item.family === "IPv4" && !item.internal && /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(item.address)) return item.address;
    }
  }
  return null;
}

async function startSite(address, spec) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-site-"));
  fs.mkdirSync(path.join(directory, "recordings"));
  const env = {
    PATH: process.env.PATH, PYTHONPATH: ROOT, NVR_DB_PATH: path.join(directory, "state.sqlite3"), NVR_RECORDINGS_PATH: path.join(directory, "recordings"),
    NVR_CREDENTIAL_KEY_PATH: path.join(directory, "credentials.key"), NVR_COOKIE_SECURE: "false",
  };
  const server = spawn("python", ["-m", "uvicorn", "tests.fixtures.site_app:app", "--host", address, "--port", String(PORT), "--log-level", "warning"], { cwd: ROOT, env, stdio: "ignore" });
  const url = `http://${address}:${PORT}`;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(`${url}/healthz`)).ok) break;
    } catch { /* not yet */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  const created = await fetch(`${url}/api/setup/admin`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: "site-admin", password: "site-admin-password-1234" }) });
  if (created.status !== 201) throw new Error(`The site could not be set up: ${created.status}`);
  const cookie = created.headers.getSetCookie().map(item => item.split(";")[0]).join("; ");
  const seeded = spawnSync("python", ["-m", "tests.fixtures.seed_site", JSON.stringify(spec)], { cwd: ROOT, env, encoding: "utf8", timeout: 60_000 });
  if (seeded.status !== 0) throw new Error(`The site could not be seeded: ${seeded.stderr}`);
  const token = await fetch(`${url}/api/admin/api-tokens`, { method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie }, body: JSON.stringify({ name: "console", scopes: ["read", "media"] }) });
  const secret = (await token.json()).secret;
  return { url, secret, made: JSON.parse(seeded.stdout.trim().split("\n").pop()), stop() { server.kill("SIGTERM"); fs.rmSync(directory, { recursive: true, force: true }); } };
}

async function addSite(page, site, name) {
  await page.goto("/settings/sites");
  const toggle = page.getByLabel("This NVR is a console");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  await expect(toggle).toBeChecked();
  const form = page.getByRole("form", { name: "Add a site" });
  await form.getByLabel("Site name").fill(name);
  await form.getByLabel("Site address").fill(site.url);
  await form.getByLabel("Site token").fill(site.secret);
  await form.getByLabel("The site is on a private network address").check();
  await form.getByRole("button", { name: "Add site" }).click();
  await expect(page.getByText("Site added.")).toBeVisible();
  await expect(page.getByRole("list", { name: "Site list" })).toContainText(`${name}`);
  await expect(page.getByRole("list", { name: "Site list" })).toContainText("Reachable");
}

test("an administrator federates a site and its cameras and alarms appear, with alarms handled at the site", async ({ page }) => {
  const address = process.env.NVR_SITE_ADDRESS || privateAddress();
  test.skip(!address, "needs a private network address of this machine");
  seedFixtures(1);
  const site = await startSite(address, { cameras: ["Gate A1", "Gate A2"], group: "Gates", rule: "Gate entry" });
  try {
    await signInNext(page);
    await addSite(page, site, "Alpha");
    // The token is never shown again, and an administrator-scoped token would have been refused.
    expect(await page.content()).not.toContain(site.secret);

    await page.getByRole("link", { name: "Live" }).click();
    await expect(page.getByRole("button", { name: /^Gate A1\b/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Gate A2\b/ })).toBeVisible();
    await page.getByRole("tab", { name: "Groups" }).or(page.getByRole("button", { name: "Groups" })).first().click();
    await expect(page.getByText("Alpha").first()).toBeVisible();  // Each site is a group of cameras

    // The alarm the site raised is in the inbox here; it is acknowledged at the site, not here.
    const chip = page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i });
    await expect(chip).toHaveText(/1 new alarm/, { timeout: 30_000 });
    await chip.click();
    const inbox = page.getByRole("dialog", { name: "Alarms" });
    await inbox.getByRole("list", { name: "Alarms" }).getByRole("button", { name: /^Gate entry: Vehicle on Gate A1/ }).click();
    const detail = inbox.getByRole("article", { name: "Alarm: Gate entry" });
    await expect(detail).toBeVisible();
    await detail.getByRole("button", { name: "Acknowledge" }).click();
    await expect(detail).toContainText("belongs to the site Alpha");
  } finally {
    site.stop();
  }
});

test("live video of a site's camera plays through the console", async ({ page }) => {
  test.skip(!process.env.CI, "needs the simulated RTSP camera of the CI workflow");
  const address = process.env.NVR_SITE_ADDRESS || privateAddress();
  test.skip(!address, "needs a private network address of this machine");
  test.setTimeout(120_000);
  seedFixtures(1);
  const site = await startSite(address, { cameras: ["Gate A1"], group: "Gates", stream: "rtsp://127.0.0.1:8554/nvr-test" });
  try {
    await signInNext(page);
    await addSite(page, site, "Alpha");
    await page.getByRole("link", { name: "Live" }).click();
    await page.getByRole("button", { name: /^Gate A1\b/ }).click();
    await expect(page.getByRole("group", { name: /^Gate A1:/ })).toHaveAttribute("data-phase", "live", { timeout: 45_000 });
  } finally {
    site.stop();
  }
});
