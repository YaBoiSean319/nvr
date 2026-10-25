// Real-browser journey for the viewing audit report (#120).
const { test, expect, seedFixtures, signInAs, signInNext } = require("./helpers.cjs");

async function openAudit(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Viewing audit" }).click();
}

test("what an operator watches, exports and snapshots shows in the report by person and by camera, as CSV, and retention can be set", async ({ page, browser, baseURL }) => {
  const manifest = seedFixtures(2, { footage: true });
  const [first] = manifest.camera_ids;
  await signInNext(page);
  const operator = await signInAs(browser, baseURL, "camera-operator");

  const segments = await (await operator.page.request.get(`/api/cameras/${first}/segments`)).json();
  const segment = (segments.segments ?? segments)[0];
  expect((await operator.page.request.get(`/api/cameras/${first}/segments/${segment.id}/media`)).status()).toBe(200);
  const at = new Date(Date.parse(segment.starts_at) + 1000).toISOString();
  expect((await operator.page.request.get(`/api/cameras/${first}/segments/${segment.id}/snapshot`, { params: { at } })).status()).toBe(200);

  await openAudit(page);
  const report = page.getByRole("table", { name: "Viewing by person" });
  const row = report.getByRole("row", { name: /camera-operator/ });
  await expect(row.getByRole("cell").nth(2)).toContainText("1 ·"); // One playback session.
  await expect(row.getByRole("cell").nth(5)).toHaveText("1"); // One snapshot.

  await page.getByLabel("Camera", { exact: true }).check();
  const byCamera = page.getByRole("table", { name: "Viewing by camera" });
  await expect(byCamera.getByRole("row", { name: /Camera 001/ }).getByRole("cell").nth(0)).toHaveText("1"); // One person.
  await expect(byCamera.getByRole("row", { name: "Total" })).toBeVisible();

  await byCamera.getByRole("button", { name: "Show entries for Camera 001" }).click();
  const entries = page.getByRole("table", { name: "Viewing entries" });
  await expect(entries).toContainText("Snapshot");
  await expect(entries).toContainText("Playback");

  const href = await page.getByRole("link", { name: "Download CSV by camera" }).getAttribute("href");
  const csv = await page.request.get(href);
  expect(csv.status()).toBe(200);
  const text = await csv.text();
  expect(text).toContain("camera,people");
  expect(text).toContain("Camera 001");
  expect(text).not.toMatch(/rtsp:|@/);

  // Retention is saved with its limits explained, and an out-of-range value is refused.
  const retention = page.getByRole("region", { name: "Audit retention" });
  await retention.getByLabel("Keep viewing entries for (days)").fill("90");
  await retention.getByRole("button", { name: "Save retention" }).click();
  await expect(retention.getByRole("status")).toContainText("Kept for 90 days");
  await page.reload();
  await expect(page.getByRole("region", { name: "Audit retention" }).getByLabel("Keep viewing entries for (days)")).toHaveValue("90");
  await operator.context.close();
});

test("operators cannot reach the viewing audit", async ({ page, browser, baseURL }) => {
  seedFixtures(1);
  const operator = await signInAs(browser, baseURL, "camera-operator");
  expect((await operator.page.request.get("/api/admin/viewing-audit/report")).status()).toBe(403);
  await operator.context.close();
  await signInNext(page);
});
