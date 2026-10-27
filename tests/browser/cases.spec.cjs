// Real-browser journeys for case files (#129) on generated footage: camera 1 records 0-60 s from the anchor.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { test, expect, seedFixtures, signInNext, signInAs } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

async function startInPlayback(page) {
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await nav(page).getByRole("link", { name: "Playback" }).click();
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByLabel("Playback time")).toHaveAttribute("data-time", String(anchor));
  return { fixtures, anchor };
}

async function heldSegments(page, cameraId) {
  const response = await page.request.get(`/api/cameras/${cameraId}/segments`);
  const segments = (await response.json()).segments ?? [];
  return segments.filter(segment => segment.preserved).length;
}

test("a clip added from Playback is held, the case builds an evidence package that verifies, and closing releases the footage", async ({ page, browser, baseURL }) => {
  test.setTimeout(240_000);
  const { fixtures } = await startInPlayback(page);
  const cameraId = fixtures.camera_ids[0];
  expect(await heldSegments(page, cameraId)).toBe(0);

  // Mark a stretch of footage and add it to a new case.
  await page.getByRole("button", { name: "Add to case…" }).click();
  const dialog = page.getByRole("dialog", { name: "Add to a case" });
  await dialog.getByLabel("New case title").fill("Gate break-in");
  await dialog.getByLabel("Label (optional)").fill("Door opens");
  await dialog.getByRole("button", { name: "Add clip" }).click();
  await expect(dialog.getByRole("status")).toContainText(/Clip added\. \d+ recorded intervals? (is|are) now held\./);
  await dialog.getByRole("button", { name: "Done" }).click();
  expect(await heldSegments(page, cameraId)).toBeGreaterThan(0);

  // The Cases list is reachable from Playback.
  await page.getByRole("link", { name: "Cases", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Cases" })).toBeVisible();
  const list = page.getByRole("list", { name: "Cases" });
  await expect(list).toContainText("Gate break-in");
  await expect(list).toContainText("1 clip");
  await list.getByRole("link", { name: /Gate break-in/ }).click();

  const items = page.getByRole("list", { name: "Case items" });
  await expect(items).toContainText("Camera 001");
  await expect(items).toContainText("Door opens");
  await page.getByRole("textbox", { name: "Add a note" }).fill("Door forced at 02:10");
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(items).toContainText("Door forced at 02:10");
  await expect(items.getByRole("listitem").filter({ hasText: "Door forced" }).getByRole("button", { name: /Remove/ })).toHaveCount(0); // Notes stay

  // Another person is not in the case until they are added.
  const { context: operator, page: operatorPage } = await signInAs(browser, baseURL, "camera-operator");
  const caseId = page.url().split("/").pop();
  expect((await operatorPage.request.get(`/api/cases/${caseId}`)).status()).toBe(404);
  await page.getByRole("textbox", { name: "Add a member by user name" }).fill("camera-operator");
  await page.getByRole("button", { name: "Add member" }).click();
  await expect(page.getByRole("list", { name: "Case members" })).toContainText("camera-operator");
  expect((await operatorPage.request.get(`/api/cases/${caseId}`)).status()).toBe(200);
  await operator.close();

  // The evidence package is built in the background; it holds the clip, the notes and the audit, and verifies.
  await page.getByRole("button", { name: "Build evidence package" }).click();
  const packages = page.getByRole("list", { name: "Evidence packages" });
  const download = packages.getByRole("link", { name: "Download" });
  await expect(download).toBeVisible({ timeout: 180_000 });
  const [file] = await Promise.all([page.waitForEvent("download"), download.click()]);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-case-"));
  const target = path.join(directory, "case.zip");
  await file.saveAs(target);
  const names = JSON.parse(execFileSync("python", ["-c", "import json,sys,zipfile; print(json.dumps(zipfile.ZipFile(sys.argv[1]).namelist()))", target], { encoding: "utf8" }));
  expect(names).toEqual(expect.arrayContaining(["case.json", "notes.md", "notes.json", "audit.json", "metadata.json", "integrity.json"]));
  expect(names.filter(name => name.startsWith("clips/"))).toHaveLength(1);
  await packages.locator("input[type=file]").setInputFiles(target);
  await expect(packages.getByRole("status")).toContainText("The files match their manifest and its clip package verifies.");
  fs.rmSync(directory, { recursive: true, force: true });

  // Closing needs a confirmation that says what is released.
  await page.getByRole("button", { name: "Close case…" }).click();
  const confirm = page.getByRole("dialog", { name: "Close this case?" });
  await expect(confirm).toContainText("releases the hold on");
  await confirm.getByRole("button", { name: "Keep it open" }).click();
  expect(await heldSegments(page, cameraId)).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Close case…" }).click();
  await confirm.getByRole("button", { name: "Close case and release holds" }).click();
  await expect(page.locator(".case-status")).toContainText("Closed");
  expect(await heldSegments(page, cameraId)).toBe(0);
  await expect(page.getByRole("textbox", { name: "Add a note" })).toHaveCount(0);

  // The audit trail names what happened.
  await page.getByRole("button", { name: "Show the audit trail" }).click();
  const trail = page.getByRole("list", { name: "Audit trail" });
  await expect(trail).toContainText("clip added");
  await expect(trail).toContainText("closed");
});

test("someone who is not in a case cannot open it, and the list shows only their own", async ({ page, browser, baseURL }) => {
  await startInPlayback(page);
  await page.getByRole("link", { name: "Cases", exact: true }).click();
  await page.getByRole("textbox", { name: "Title" }).fill("Private case");
  await page.getByRole("button", { name: "Start case" }).click();
  await expect(page.getByRole("heading", { name: "Private case" })).toBeVisible();
  const caseId = page.url().split("/").pop();

  const { context, page: operator } = await signInAs(browser, baseURL, "camera-operator");
  await operator.goto("/cases");
  await expect(operator.getByRole("heading", { name: "Cases" })).toBeVisible();
  await expect(operator.getByText("No cases yet.")).toBeVisible();
  await operator.goto(`/cases/${caseId}`);
  await expect(operator.getByRole("alert")).toContainText("Case not found");
  await context.close();
});
