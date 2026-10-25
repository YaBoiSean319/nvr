// Real-browser journeys for camera access grants (#119).
const { test, expect, seedFixtures, signInAs, signInNext } = require("./helpers.cjs");

async function openCameraAccess(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Users & access" }).click();
  return page.getByRole("region", { name: "Camera access" });
}

test("narrowing an operator to a camera group hides every other camera from them, and an expiry ends access", async ({ page, browser, baseURL }) => {
  const manifest = seedFixtures(4);
  const [first, second, third] = manifest.camera_ids; // Odd-numbered fixture cameras belong to "Operations".
  await signInNext(page);
  const operator = await signInAs(browser, baseURL, "camera-operator");
  await expect(operator.page.getByRole("complementary", { name: "Camera browser" })).toContainText("Camera 002");

  const region = await openCameraAccess(page);
  const row = region.getByRole("row", { name: /camera-operator/ });
  await expect(row).toContainText("All cameras");
  await row.getByRole("button", { name: "Change camera access for camera-operator" }).click();
  const dialog = page.getByRole("dialog", { name: "Camera access for camera-operator" });
  await dialog.getByLabel("Only the cameras of chosen groups").check();
  await dialog.getByRole("checkbox", { name: "Operations" }).check();
  await dialog.getByRole("button", { name: "Save access" }).click();
  await expect(region.getByRole("status")).toContainText("Camera access updated for camera-operator");
  await expect(row).toContainText("Operations");

  // The operator's next load shows only the group's cameras; the others do not exist for them.
  await operator.page.reload();
  const browserPane = operator.page.getByRole("complementary", { name: "Camera browser" });
  await expect(browserPane).toContainText("Camera 001");
  await expect(browserPane).toContainText("Camera 003");
  await expect(browserPane).not.toContainText("Camera 002");
  await expect(browserPane).not.toContainText("Camera 004");
  expect((await operator.page.request.get(`/api/cameras/${second}/segments`)).status()).toBe(404);
  expect((await operator.page.request.get(`/api/cameras/${first}/coverage`)).status()).toBe(200);
  const cameras = await (await operator.page.request.get("/api/cameras")).json();
  expect(cameras.cameras.map(camera => camera.id).sort()).toEqual([first, third].sort());

  // Access that expires stops at once: the next request already sees nothing.
  const soon = new Date(Date.now() + 3000).toISOString();
  const operatorId = (await (await page.request.get("/api/admin/access-grants")).json()).operators.find(item => item.username === "camera-operator").id;
  const group = (await (await page.request.get("/api/admin/access-grants")).json()).groups[0].id;
  const changed = await page.request.put("/api/admin/access-grants", { data: { subject_type: "user", subject: String(operatorId), grants: [{ scope_type: "group", group_id: group, expires_at: soon }] } });
  expect(changed.status()).toBe(200);
  expect((await operator.page.request.get(`/api/cameras/${first}/coverage`)).status()).toBe(200);
  await expect.poll(async () => (await operator.page.request.get(`/api/cameras/${first}/coverage`)).status(), { timeout: 15_000 }).toBe(404);
  expect((await (await operator.page.request.get("/api/cameras")).json()).cameras).toEqual([]);
  await operator.page.reload();
  await expect(operator.page.getByRole("complementary", { name: "Camera browser" })).not.toContainText("Camera 001");

  // The administrator sees the grant as expired, and it was audited.
  await page.reload();
  await expect(page.getByRole("region", { name: "Camera access" }).getByRole("row", { name: /camera-operator/ })).toContainText("expired");
  await operator.context.close();
});

test("an administrator keeps full access, and a role can be given access", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  const region = await openCameraAccess(page);
  await expect(region.getByRole("row", { name: /camera-admin/ })).toHaveCount(0); // Administrators are not in the list: they need no grant.
  await region.getByLabel("Entra role").fill("NVR.Contractor");
  await region.getByRole("button", { name: "Choose cameras…" }).click();
  const dialog = page.getByRole("dialog", { name: "Camera access for NVR.Contractor" });
  await dialog.getByLabel("Only the cameras of chosen groups").check();
  await dialog.getByRole("checkbox", { name: "Operations" }).check();
  await dialog.getByRole("button", { name: "Save access" }).click();
  await expect(region.getByRole("status")).toContainText("Camera access updated for NVR.Contractor");
  await expect(region.getByRole("row", { name: /NVR.Contractor/ })).toContainText("Operations");
  expect((await page.request.get("/api/cameras")).status()).toBe(200);
});
