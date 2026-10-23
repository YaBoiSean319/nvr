// Real-browser journeys for bulk changes and Groups & Policies (#102).
// The bulkTrigger fixture makes the database reject a recording-enablement change for camera 2.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const inventory = page => page.getByRole("region", { name: "Camera inventory" });
const dialog = page => page.getByRole("dialog");

async function openCameras(page) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await expect(inventory(page)).toBeVisible();
}

async function check(page, ...names) {
  for (const name of names) await inventory(page).getByRole("checkbox", { name: `Select ${name} for bulk changes` }).check();
}

async function cameras(page) {
  return (await (await page.request.get("/api/cameras")).json()).cameras;
}

test("a reviewed bulk change applies to exactly the reviewed cameras, reports each one and retries only failures", async ({ page }) => {
  const fixtures = seedFixtures(4, { bulkTrigger: true });
  await signInNext(page);
  await page.request.delete(`/api/cameras/${fixtures.camera_ids[3]}`); // archive camera 4
  await openCameras(page);
  await expect(inventory(page).getByRole("button", { name: "Bulk change…" })).toHaveCount(0);

  await check(page, "Camera 001", "Camera 002", "Camera 003");
  const bulk = inventory(page).getByRole("group", { name: "Bulk selection" });
  await expect(bulk).toContainText("3 selected for bulk changes");
  await inventory(page).getByRole("searchbox").fill("Camera 001");
  await expect(bulk).toContainText("(2 hidden by filters)");
  await inventory(page).getByLabel("Archived cameras").selectOption("all");
  await inventory(page).getByRole("searchbox").fill("");
  await bulk.getByRole("button", { name: "Select all 4 matching" }).click();
  await expect(bulk).toContainText("4 selected for bulk changes");

  await bulk.getByRole("button", { name: "Bulk change…" }).click();
  await dialog(page).getByRole("combobox", { name: "Recording", exact: true }).selectOption("on");
  await dialog(page).getByRole("button", { name: "Review 4 cameras" }).click();
  const table = dialog(page).getByRole("table", { name: "Cameras in this change" });
  await expect(table.locator("tbody tr")).toHaveCount(4);
  await expect(table.locator('tr[data-outcome="excluded"]')).toContainText("Archived cameras are excluded from bulk changes.");

  // Filters changing after review do not change what is applied.
  let submitted;
  page.on("request", request => request.url().endsWith("/api/cameras/bulk-update") && (submitted = request.postDataJSON()));
  await dialog(page).getByRole("button", { name: "Apply to 3 cameras" }).click();
  await expect(dialog(page).getByRole("status")).toHaveText("2 changed, 1 not changed, 1 excluded.");
  expect(submitted.camera_ids).toEqual(fixtures.camera_ids.slice(0, 3));
  await expect(table.locator('tr[data-outcome="failed"]')).toContainText("Camera 002");
  const after = await cameras(page);
  expect(after.filter(camera => camera.recording_enabled).map(camera => camera.name).sort()).toEqual(["Camera 001", "Camera 003"]);

  await dialog(page).getByRole("button", { name: "Retry 1 failed" }).click();
  await expect(dialog(page).getByRole("status")).toHaveText("2 changed, 1 not changed, 1 excluded.");
  expect(submitted.camera_ids).toEqual([fixtures.camera_ids[1]]);
});

test("a lost response is reconciled before anything is called failed, and closing never claims a cancel", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  await openCameras(page);
  await check(page, "Camera 001", "Camera 002");
  await inventory(page).getByRole("button", { name: "Bulk change…" }).click();
  await dialog(page).getByLabel("Recording mode").selectOption("motion");
  await dialog(page).getByRole("button", { name: "Review 2 cameras" }).click();

  // The NVR applies the change but the answer never arrives.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/api/cameras/bulk-update", async route => {
    await route.fetch();
    await held;
    await route.abort("connectionreset");
  });
  await dialog(page).getByRole("button", { name: "Apply to 2 cameras" }).click();
  await expect(dialog(page).getByText("Applying… Closing this dialog does not stop the change.")).toBeVisible();
  await dialog(page).getByRole("button", { name: "Hide (keeps running)" }).click();
  await expect(dialog(page)).toHaveCount(0);
  await expect(inventory(page).getByText("Bulk change running…")).toBeVisible();
  await expect(page.getByText(/cancel/i)).toHaveCount(0);
  release();
  await expect(inventory(page).getByText("Bulk change finished.")).toBeVisible();
  await inventory(page).getByRole("button", { name: "View results" }).click();
  await expect(dialog(page).getByRole("status")).toHaveText("2 changed, 0 not changed.");
  await expect(dialog(page).getByRole("table", { name: "Cameras in this change" })).toContainText("Confirmed after an unclear response.");
});

test("groups edit name, browsing members and policies, and members inherit them", async ({ page }) => {
  const fixtures = seedFixtures(4);
  await signInNext(page);
  await openCameras(page);
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Groups & Policies" }).click();
  const groups = page.getByRole("region", { name: "Policy groups" });
  await groups.getByLabel("New group name").fill("Night shift");
  await groups.getByRole("button", { name: "Create group" }).click();
  const editor = page.getByRole("region", { name: "Policies for Night shift" });
  await expect(editor).toBeVisible();

  await groups.getByRole("button", { name: /^Operations/ }).click();
  const operations = page.getByRole("region", { name: "Policies for Operations" });
  const members = operations.getByRole("region", { name: "Policy members" });
  await expect(members).toContainText("Camera 001");
  await expect(members).toContainText("Camera 003");
  await operations.getByLabel("Camera 002").check();
  await operations.getByLabel("Name", { exact: true }).fill("Operations East");
  await operations.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Policies for Operations East" }).getByRole("status")).toHaveText("Saved name and browsing members.");
  const listed = (await (await page.request.get("/api/camera-groups")).json()).groups.find(group => group.id === fixtures.group_id);
  expect(listed.camera_ids.sort()).toEqual([fixtures.camera_ids[0], fixtures.camera_ids[1], fixtures.camera_ids[2]].sort());

  const east = page.getByRole("region", { name: "Policies for Operations East" });
  // Dirty sections ask before switching.
  await east.getByRole("tab", { name: "Retention" }).click();
  await east.getByLabel("Keep members' footage for a specific time").check();
  await east.getByLabel("Days to keep footage").fill("45");
  await east.getByRole("tab", { name: "Schedule" }).click();
  await page.getByRole("dialog", { name: "Unsaved changes" }).getByRole("button", { name: "Save" }).click();
  await expect(east.getByRole("tab", { name: "Schedule" })).toHaveAttribute("aria-selected", "true");
  await east.getByLabel("Members follow a weekly schedule").check();
  await east.getByRole("button", { name: "Add interval" }).click();
  for (const day of ["Monday", "Tuesday", "Wednesday"]) await east.getByLabel(`${day} in interval 1`).check();
  await east.getByRole("button", { name: "Save" }).click();
  await expect(east.getByRole("status")).toContainText("Saved schedule.");
  await east.getByRole("tab", { name: "Storage" }).click();
  await east.getByLabel("Record members' footage to").selectOption("archive");
  await east.getByRole("button", { name: "Save" }).click();
  await expect(east.getByRole("status")).toContainText("Saved storage volume.");

  // A member without overrides now shows the group's policy as its effective policy.
  const retention = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[2]}/retention`)).json();
  expect([retention.retention_days, retention.policy_source]).toEqual([45, "group"]);
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Cameras", exact: true }).click();
  await inventory(page).getByRole("button", { name: "Camera 003", exact: true }).click();
  await page.getByRole("tab", { name: "Schedule & Retention" }).click();
  await expect(page.getByRole("form", { name: "Schedule and retention settings" }).getByRole("status").first())
    .toContainText("Retention: 45 days from its group");
  await expect(page.getByRole("form", { name: "Schedule and retention settings" }).getByRole("status").first())
    .toContainText("Schedule from its group");
});

test("operators cannot reach bulk changes or group policies, and the server refuses them", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page, "camera-operator");
  await openCameras(page);
  await expect(inventory(page).getByRole("checkbox", { name: /for bulk changes/ })).toHaveCount(0);
  await page.goto("/next/cameras/groups");
  await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  expect((await page.request.post("/api/cameras/bulk-update", { data: { camera_ids: [fixtures.camera_ids[0]], recording_enabled: true } })).status()).toBe(403);
  expect((await page.request.put(`/api/camera-groups/${fixtures.group_id}/retention`, { data: { retention_days: 5 } })).status()).toBe(403);
});
