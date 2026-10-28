// Real-browser journeys for event digests (#136): a scheduled digest with working links, one made on demand, and the administrator's schedule form.
const { test, expect, seedFixtures, seedDigest, signInNext } = require("./helpers.cjs");

test("a scheduled digest lists the alarms, the gap and the incident, and each link opens its record", async ({ page }) => {
  seedFixtures(2);
  const seeded = seedDigest();
  await signInNext(page);
  await page.getByRole("link", { name: "Digests" }).click();
  await page.getByRole("link", { name: /Night shift/ }).click();

  const digestUrl = page.url();
  const digest = page.getByRole("article", { name: "Night shift" });
  await expect(page.getByTestId("digest-headline")).toContainText("3 alarms (2 not resolved)");
  await expect(page.getByTestId("digest-headline")).toContainText("1 system incident");
  await expect(digest.getByRole("table", { name: "Alarms by rule" })).toContainText("Front gate");
  await expect(digest.getByRole("list", { name: "Alarms" }).getByRole("link")).toHaveCount(3);
  await expect(digest.getByRole("list", { name: "Vehicles at gates" }).getByRole("link")).toHaveCount(1);
  await expect(digest.getByRole("list", { name: "Recording gaps" })).toContainText("Camera 001");
  await expect(digest.getByRole("list", { name: "System incidents" })).toContainText("Camera 001 stream down");
  await expect(page.getByTestId("digest-unanalysed")).toContainText("Not all of this time was analysed");

  // An alarm link opens that alarm; a gap link opens Playback on the camera; an incident link opens the incidents.
  const alarmLinks = digest.getByRole("list", { name: "Alarms" }).getByRole("link");
  expect(await alarmLinks.first().getAttribute("href")).toMatch(/^\/\?alarm=/);
  await alarmLinks.first().click();
  await expect(page.getByRole("dialog", { name: "Alarms" })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Alarms" })).toContainText("Front gate");
  await page.keyboard.press("Escape");

  await page.goto(digestUrl);
  const gap = page.getByRole("article", { name: "Night shift" }).getByRole("list", { name: "Recording gaps" }).getByRole("link").filter({ hasText: "1 h 0 min" });
  expect(await gap.getAttribute("href")).toContain(`/playback?playback=${seeded.camera_id}&at=`);
  await gap.click();
  await expect(page).toHaveURL(/\/playback/);
  await expect(page.getByRole("group", { name: /^Camera 001/ })).toBeVisible();

  await page.goto(digestUrl);
  await page.getByRole("article", { name: "Night shift" }).getByRole("list", { name: "System incidents" }).getByRole("link").click();
  await expect(page.getByRole("dialog", { name: "Operational status" })).toBeVisible();
});

test("a digest can be made on demand for a range", async ({ page }) => {
  seedFixtures(2);
  seedDigest();
  await signInNext(page);
  await page.goto("/digests");
  await page.getByRole("form", { name: "Make a digest" }).getByRole("button", { name: "Make digest" }).click();
  await expect(page.getByTestId("digest-headline")).toContainText("3 alarms", { timeout: 15_000 });
  await expect(page.getByRole("list", { name: "Alarms" }).getByRole("link")).toHaveCount(3);
});

test("an administrator schedules a digest and sends it through an action, and the secrets are not shown again", async ({ page }) => {
  seedFixtures(2);
  seedDigest();
  await signInNext(page);
  await page.goto("/settings/digests");
  await page.getByRole("button", { name: "New schedule" }).click();
  const form = page.getByRole("form", { name: "New schedule" });
  await form.getByLabel("Name").fill("Evening shift");
  await form.getByLabel("For").selectOption({ label: "camera-admin" });
  await form.getByRole("button", { name: "Add an action" }).click();
  await form.getByRole("group", { name: /^Action/ }).getByLabel("Name").fill("Control room");
  await form.getByLabel("Destination address").fill("https://8.8.8.8/hook");
  await form.getByLabel("Signing secret").fill("a-long-enough-signing-secret");
  await form.getByRole("button", { name: "Save schedule" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  const row = page.getByRole("list", { name: "Schedules" }).getByRole("listitem").filter({ hasText: "Evening shift" });
  await expect(row).toContainText("for camera-admin");
  expect(await page.content()).not.toContain("a-long-enough-signing-secret");

  await row.getByRole("button", { name: "Make last shift's digest now" }).click();
  await expect(page.getByRole("status").filter({ hasText: "queued it for delivery" })).toBeVisible();
  await row.getByRole("button", { name: "Deliveries" }).click();
  await expect(page.getByRole("table", { name: "Deliveries of Evening shift" })).toContainText("Control room");

  await row.getByRole("button", { name: "Edit Evening shift" }).click();
  await expect(page.getByLabel("Destination address")).toHaveAttribute("placeholder", /Saved/);
  await expect(page.getByLabel("Destination address")).toHaveValue("");
});
