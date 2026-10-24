// Real-browser journeys for export marks, the export drawer and background jobs (#101).
// Fixture offsets from the anchor (seconds): camera 1 records 0-60 and 150-210; camera 2 records 30-90.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

function row(page, name) {
  return page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
}

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

async function clockTime(page) {
  return Number(await page.getByLabel("Playback time").getAttribute("data-time"));
}

async function investigate(page, cameras = [1, 2]) {
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await nav(page).getByRole("link", { name: "Playback" }).click();
  for (const index of cameras) await row(page, `Camera ${String(index).padStart(3, "0")}`).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => clockTime(page)).toBe(anchor);
  return { fixtures, anchor };
}

async function seekTo(page, anchor, seconds) {
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  const now = await clockTime(page);
  const presses = Math.round((anchor + seconds * 1000 - now) / 5000);
  for (let step = 0; step < Math.abs(presses); step += 1) await ruler.press(presses < 0 ? "ArrowLeft" : "ArrowRight");
  await expect.poll(() => clockTime(page)).toBe(anchor + seconds * 1000);
}

/** Tag the playing video elements so a remount would be detected. */
async function tagPlayers(page) {
  await page.locator(".playback-tile video").evaluateAll(videos => { window.__players = videos; });
}

async function samePlayers(page) {
  return page.locator(".playback-tile video").evaluateAll(videos =>
    videos.length === window.__players.length && videos.every((video, index) => video === window.__players[index]));
}

test("search → marks → export → authenticated download of individual and grid files", async ({ page, browser }) => {
  test.setTimeout(180_000);
  const { anchor } = await investigate(page);
  await tagPlayers(page);
  await seekTo(page, anchor, 30);
  await page.getByRole("button", { name: "Mark in" }).click();
  await seekTo(page, anchor, 40);
  await page.getByRole("button", { name: "Mark out" }).click();

  // Completing the range opens the drawer beside Playback without touching the players.
  const drawer = page.getByRole("complementary", { name: "Export footage" });
  await expect(drawer).toBeVisible();
  expect(await samePlayers(page), "players were remounted when the drawer opened").toBe(true);
  expect(await clockTime(page), "the playhead moved when the drawer opened").toBe(anchor + 40_000);
  const timeline = page.getByRole("region", { name: "Timeline" });
  await expect(timeline).toHaveAttribute("data-mark-in", String(anchor + 30_000));
  await expect(timeline).toHaveAttribute("data-mark-out", String(anchor + 40_000));
  await expect(page.getByTestId("export-range")).toBeVisible();
  await expect(drawer.getByRole("checkbox", { name: "Individual camera files" })).toBeChecked();
  await expect(drawer.getByRole("checkbox", { name: "Synchronized grid" })).toBeChecked();
  await expect(drawer.getByRole("radio", { name: "1080p" })).toBeChecked();
  await expect(drawer.getByText("Known missing footage")).toHaveCount(0);

  await drawer.getByRole("button", { name: "Export", exact: true }).click();
  await expect(drawer.getByText("Export queued.")).toBeVisible();
  await drawer.getByRole("button", { name: "View export jobs" }).click();
  const jobs = page.getByRole("list", { name: "Export jobs" });
  const job = jobs.getByRole("listitem").first();
  const download = job.getByRole("link", { name: "Download" });
  await expect(download).toBeVisible({ timeout: 150_000 });
  await expect(job).toHaveAttribute("data-status", "completed");

  const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-export-"));
  const target = path.join(directory, "export.zip");
  await file.saveAs(target);
  const names = JSON.parse(execFileSync("python", ["-c", "import json,sys,zipfile; print(json.dumps(zipfile.ZipFile(sys.argv[1]).namelist()))", target], { encoding: "utf8" }));
  fs.rmSync(directory, { recursive: true, force: true });
  expect(names, "package contents").toContain("grid.mp4");
  expect(names.filter(name => /^camera-.*\.mp4$/.test(name))).toHaveLength(2);
  expect(names).toContain("integrity.json");

  // The package needs the owner's session.
  const href = await download.getAttribute("href");
  const anonymous = await browser.newContext();
  expect((await anonymous.request.get(new URL(href, page.url()).toString())).status()).toBe(401);
  await anonymous.close();
});

test("opening and closing the drawer keeps range, playhead, play state, layout and marks", async ({ page }) => {
  const { anchor } = await investigate(page);
  await seekTo(page, anchor, 35);
  await page.getByRole("button", { name: "Mark in" }).click();
  await tagPlayers(page);
  const columns = await page.locator(".playback-video .grid").getAttribute("data-columns");
  await page.getByRole("button", { name: "Play", exact: true }).click();

  await page.getByRole("button", { name: "Export…" }).click();
  const drawer = page.getByRole("complementary", { name: "Export footage" });
  await expect(drawer).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  // One mark: the other end is the search boundary.
  await expect(drawer.getByText("Search end")).toBeVisible();
  await drawer.getByRole("checkbox", { name: "Camera 002" }).uncheck();
  await drawer.getByRole("button", { name: "Close export" }).click();
  await expect(drawer).toHaveCount(0);
  await page.getByRole("button", { name: "Export…" }).click();
  await expect(drawer.getByRole("checkbox", { name: "Camera 002" })).not.toBeChecked();
  await expect(drawer.getByRole("checkbox", { name: "Camera 001" })).toBeChecked();

  expect(await samePlayers(page), "players were remounted by the drawer").toBe(true);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  expect(await clockTime(page)).toBeGreaterThan(anchor + 35_000);
  await expect(page.locator(".playback-video .grid")).toHaveAttribute("data-columns", columns);
  await expect(page.getByRole("region", { name: "Timeline" })).toHaveAttribute("data-mark-in", String(anchor + 35_000));
  await expect(page.getByRole("form", { name: "Footage search" })).toHaveAttribute("data-stale", "false");
});

test("invalid, out-of-range and empty exports are refused with clear messages, and missing footage is warned", async ({ page }) => {
  const { anchor } = await investigate(page, [1, 2, 3]);
  await seekTo(page, anchor, 20);
  await page.getByRole("button", { name: "Mark in" }).click();
  await seekTo(page, anchor, 100);
  await page.getByRole("button", { name: "Mark out" }).click();
  const drawer = page.getByRole("complementary", { name: "Export footage" });
  const submit = drawer.getByRole("button", { name: "Export", exact: true });

  const warning = drawer.getByRole("status").filter({ hasText: "Known missing footage" });
  await expect(warning).toContainText("Camera 001: 0:40 missing");
  await expect(warning).toContainText("Camera 002: 0:20 missing");
  await expect(warning).toContainText("Camera 003: no footage, left out of the export");
  await expect(submit).toBeEnabled();

  const from = drawer.getByLabel("From", { exact: true });
  const to = drawer.getByLabel("To", { exact: true });
  const original = await from.inputValue();
  await from.fill(await to.inputValue());
  await expect(drawer.getByRole("alert")).toHaveText("The export end must be after its start.");
  await expect(submit).toBeDisabled();
  await from.fill("2000-01-01T00:00:05");
  await expect(drawer.getByRole("alert")).toHaveText("Export marks must stay within the searched range.");
  await from.fill(original);
  await expect(submit).toBeEnabled();

  await drawer.getByRole("checkbox", { name: "Camera 001" }).uncheck();
  await drawer.getByRole("checkbox", { name: "Camera 002" }).uncheck();
  await expect(drawer.getByRole("alert")).toHaveText("There is no footage in this range for the selected cameras, so there is nothing to export.");
  await drawer.getByRole("checkbox", { name: "Camera 003" }).uncheck();
  await expect(drawer.getByRole("alert")).toHaveText("Choose at least one camera.");
  await drawer.getByRole("checkbox", { name: "Camera 001" }).check();
  await drawer.getByRole("checkbox", { name: "Individual camera files" }).uncheck();
  await drawer.getByRole("checkbox", { name: "Synchronized grid" }).uncheck();
  await expect(drawer.getByRole("alert")).toHaveText("Choose at least one output.");

  await page.getByRole("button", { name: "Clear marks" }).click();
  await expect(page.getByTestId("export-range")).toHaveCount(0);
  await expect(drawer.getByText("Search start")).toBeVisible();
});

test("jobs can be cancelled and retried, survive reload and show in another session", async ({ page, browser }) => {
  test.setTimeout(120_000);
  const { anchor } = await investigate(page);
  await seekTo(page, anchor, 0);
  await page.getByRole("button", { name: "Mark in" }).click();
  await seekTo(page, anchor, 90);
  await page.getByRole("button", { name: "Mark out" }).click();
  const drawer = page.getByRole("complementary", { name: "Export footage" });
  await drawer.getByRole("radio", { name: "4K" }).check();
  await drawer.getByRole("button", { name: "Export", exact: true }).click();
  await expect(page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /export/i })).toHaveText(/Exporting|Exports: 1 running/);

  await drawer.getByRole("button", { name: "View export jobs" }).click();
  const jobs = page.getByRole("list", { name: "Export jobs" });
  const first = jobs.getByRole("listitem").first();
  await first.getByRole("button", { name: "Cancel" }).click();
  await expect(first).toHaveAttribute("data-status", "cancelled", { timeout: 30_000 });
  await first.getByRole("button", { name: "Retry" }).click();
  await expect(jobs.getByRole("listitem")).toHaveCount(2);
  const retried = jobs.getByRole("listitem").first();
  await expect(retried).toContainText("Retry of an earlier attempt");
  await expect(retried).toHaveAttribute("data-status", /queued|running/);
  await expect(jobs.getByRole("listitem").nth(1)).toHaveAttribute("data-status", "cancelled");
  await expect(retried).toContainText("grid 4K");
  await retried.getByRole("button", { name: "Cancel" }).click();
  await expect(retried).toHaveAttribute("data-status", "cancelled", { timeout: 30_000 });

  await page.reload();
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /export/i }).click();
  await expect(page.getByRole("list", { name: "Export jobs" }).getByRole("listitem")).toHaveCount(2);

  const context = await browser.newContext();
  const other = await context.newPage();
  await signInNext(other);
  await other.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /export/i }).click();
  await expect(other.getByRole("list", { name: "Export jobs" }).getByRole("listitem")).toHaveCount(2);
  await context.close();
});
