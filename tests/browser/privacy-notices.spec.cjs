// Real-browser journey for retention by purpose and generated notices (#139): an administrator records a purpose, sees the prompt for policies without one, and opens the notice.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("an administrator requires purposes, fills one in, and the notice follows", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/privacy");
  await page.getByLabel("Organisation").fill("Harbour Depot Ltd");
  await page.getByLabel("Contact email").fill("privacy@harbour.example");
  await page.getByLabel("Require a purpose for every retention policy").check();
  await page.getByRole("button", { name: "Save privacy settings" }).click();
  await expect(page.getByText("Saved.", { exact: true })).toBeVisible();

  // The site default has no purpose yet: the page says so, then it is filled in.
  await expect(page.getByRole("status").filter({ hasText: "needs a purpose" })).toContainText("Site default");
  await page.getByLabel("Purpose for Site default").fill("Site security");
  await page.getByLabel("Legal basis for Site default").fill("Legitimate interests");
  await page.getByRole("button", { name: "Save Site default" }).click();
  await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "needs a purpose" })).toHaveCount(0);

  // The notice is generated from the configuration, and never holds a camera address.
  const notice = await (await page.request.get("/api/privacy/notice?format=html")).text();
  expect(notice).toContain("Harbour Depot Ltd");
  expect(notice).toContain("Site security");
  expect(notice).toContain("privacy@harbour.example");
  expect(notice).not.toMatch(/rtsp:|\d+\.\d+\.\d+\.\d+/);
  await expect(page.getByRole("table", { name: "Deletion record" })).toContainText("Nothing has been deleted automatically yet.");
  await expect(page.getByRole("link", { name: "Open the camera notice" })).toHaveAttribute("href", "/api/privacy/notice?format=html");
});
