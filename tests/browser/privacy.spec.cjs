// Privacy masks (#115): an administrator draws a masked area, the editor says plainly that
// recordings on disk are not masked, and unmasked viewing needs a stated reason.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

async function openPrivacy(page, camera = "Camera 001") {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: camera, exact: true }).click();
  await page.getByRole("tab", { name: "Privacy" }).click();
}

test("an administrator masks an area and sees unmasked footage only after giving a reason", async ({ page }) => {
  const fixtures = seedFixtures(1, { alarm: true });
  const cameraId = fixtures.camera_ids[0];
  await signInNext(page);
  await openPrivacy(page);
  const form = page.getByRole("form", { name: "Privacy masks" });
  await expect(form.getByRole("note")).toContainText("No privacy masks");

  const stage = form.locator(".zone-stage");
  const box = await stage.boundingBox();
  await page.mouse.move(box.x + box.width * 0.1, box.y + box.height * 0.1);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5, { steps: 5 });
  await page.mouse.up();
  await expect(form.getByRole("button", { name: /^Zone 1:/ })).toBeVisible();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("note")).toContainText("Recordings on disk are NOT masked");
  const saved = await (await page.request.get(`/api/cameras/${cameraId}/privacy`)).json();
  expect(saved.zones).toHaveLength(1);
  expect(saved.server_enforced).toBe(true);

  // Unmasked viewing: a reason first, then media is asked for unmasked.
  const unmasked = page.getByRole("form", { name: "Reason for unmasked viewing" });
  await expect(unmasked.getByRole("button", { name: "View unmasked for 15 minutes" })).toBeDisabled();
  await unmasked.getByLabel("Reason").fill("Insurance claim for the broken window on 24 October");
  await unmasked.getByRole("button", { name: "View unmasked for 15 minutes" }).click();
  const section = page.getByRole("region", { name: "Unmasked viewing" });
  await expect(section.getByRole("status")).toContainText("You see this camera unmasked");

  const media = page.waitForRequest(request => request.url().includes(`/api/cameras/${cameraId}/segments/`) && request.url().includes("unmasked=true"));
  // The alarm opens Playback at its time, which loads the recording there.
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i }).click();
  const inbox = page.getByRole("dialog", { name: "Alarms" });
  await inbox.getByRole("button", { name: /Gate entry/ }).first().click();
  await inbox.getByRole("button", { name: "Open in Playback" }).click();
  await media;

  // Masking again goes back to masked media.
  await openPrivacy(page);
  await page.getByRole("region", { name: "Unmasked viewing" }).getByRole("button", { name: "Mask again" }).click();
  await expect(page.getByRole("form", { name: "Reason for unmasked viewing" })).toBeVisible();
});

test("operators see the masks read-only and are refused unmasked media", async ({ page }) => {
  const fixtures = seedFixtures(1, { footage: true });
  const cameraId = fixtures.camera_ids[0];
  await signInNext(page);
  expect((await page.request.put(`/api/cameras/${cameraId}/privacy`, { data: { zones: [{ x: 0, y: 0, width: 0.5, height: 0.5 }] } })).ok()).toBe(true);
  await page.context().clearCookies();
  await signInNext(page, "camera-operator");
  await openPrivacy(page);
  await expect(page.getByRole("form", { name: "Privacy masks" }).getByRole("note")).toContainText("Recordings on disk are NOT masked");
  await expect(page.getByRole("region", { name: "Unmasked viewing" })).toHaveCount(0);
  const timeline = await (await page.request.post("/api/playback/timeline", { data: { camera_ids: [cameraId], start: fixtures.footage.segments[0].starts_at, end: fixtures.footage.segments[0].ends_at } })).json();
  const segment = timeline.cameras[0].segments[0];
  expect((await page.request.get(`${segment.media_url}?unmasked=true`)).status()).toBe(403);
  expect((await page.request.post(`/api/cameras/${cameraId}/privacy/unmasked`, { data: { reason: "I just want to look at it" } })).status()).toBe(403);
});
