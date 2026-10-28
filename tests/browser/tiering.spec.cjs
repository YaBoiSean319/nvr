// Real-browser journey for storage tiering (#144): a cold tier is added (and refused when it does not work), a policy is set, old footage moves and still plays.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, expect, seedFixtures, seedTiering, signInNext } = require("./helpers.cjs");

test("an administrator adds a cold tier, sets a policy, moves old footage and it still plays", async ({ page }) => {
  seedFixtures(2);
  const seeded = seedTiering();
  const cold = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-browser-cold-"));
  await signInNext(page);
  await page.goto("/settings/tiering");
  await expect(page.getByRole("list", { name: "Tier list" })).toContainText("No cold tiers yet.");

  const form = page.getByRole("form", { name: "Add a cold tier" });
  await form.getByLabel("Tier name").fill("Nowhere");
  await form.getByLabel(/^Folder/).fill("relative/path");
  await form.getByRole("button", { name: "Add cold tier" }).click();
  await expect(page.getByRole("alert")).toContainText("absolute path");
  await form.getByLabel("Tier name").fill("Archive disk");
  await form.getByLabel(/^Folder/).fill(cold);
  await form.getByRole("button", { name: "Add cold tier" }).click();
  await expect(page.getByRole("list", { name: "Tier list" })).toContainText("Archive disk");
  await expect(page.getByRole("list", { name: "Tier list" })).toContainText(`Folder ${fs.realpathSync(cold)}`);

  await form.getByLabel("Tier name").fill("Bucket");
  await form.getByLabel("Kind of tier").selectOption("s3");
  await form.getByLabel("Object store address").fill("https://192.168.99.99:9");
  await form.getByLabel("Bucket", { exact: true }).fill("nvr-cold");
  await form.getByLabel("Access key").fill("AKIAEXAMPLE");
  await form.getByLabel("Secret key").fill("not-a-real-secret-0123456789");
  await form.getByRole("checkbox", { name: /private network address/ }).check();
  await form.getByRole("button", { name: "Add cold tier" }).click();
  await expect(page.getByRole("alert")).toContainText("The cold tier does not work");
  await expect(page.getByRole("list", { name: "Tier list" })).not.toContainText("Bucket");  // Not kept unless it works

  const policy = page.getByRole("form", { name: "Policy for default" });
  await policy.getByLabel("Footage older than (days) for default").fill("30");
  await policy.getByRole("button", { name: "Save policy for default" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await page.getByRole("button", { name: "Move some footage now" }).click();
  await expect(page.getByText("2 segments moved.")).toBeVisible({ timeout: 25_000 }); // A busy CI runner copies and verifies slowly
  await expect(page.getByRole("list", { name: "Tier list" })).toContainText("2 segments");
  expect(fs.readdirSync(path.join(fs.realpathSync(cold), seeded.segments[0].camera_id)).length).toBe(2);

  // Still there to play, through the server.
  for (const segment of seeded.segments) {
    const response = await page.request.get(`/api/cameras/${segment.camera_id}/segments/${segment.id}/media`);
    expect(response.status()).toBe(200);
    expect((await response.body()).length).toBe(segment.size);
  }
  await page.getByRole("button", { name: "Remove Archive disk" }).click();
  await expect(page.getByRole("alert")).toContainText("A policy sends footage to this tier");
});
