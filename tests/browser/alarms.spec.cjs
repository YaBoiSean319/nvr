// Real-browser journeys for the alarm inbox (#109). The alarm fixture raises one new "Gate entry"
// alarm on Camera 001, 30 seconds into its first generated recorded interval.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const inbox = page => page.getByRole("dialog", { name: "Alarms" });
const alarmChip = page => page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i });
const detail = page => inbox(page).getByRole("article", { name: "Alarm: Gate entry" });

async function openAlarm(page) {
  await alarmChip(page).click();
  await inbox(page).getByRole("list", { name: "Alarms" }).getByRole("button", { name: /^Gate entry: Vehicle on Camera 001/ }).click();
  await expect(detail(page)).toBeVisible();
}

test("an operator handles an alarm and another signed-in user sees each step without reloading", async ({ page, browser }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page, "camera-operator");
  const admin = await browser.newPage();
  await signInNext(admin);
  await expect(alarmChip(admin)).toHaveText("1 new alarm");
  await openAlarm(admin);

  await expect(alarmChip(page)).toHaveText("1 new alarm");
  await openAlarm(page);
  // The snapshot and clip load from the recorded footage.
  await expect.poll(() => detail(page).getByRole("img").evaluate(image => image.complete && image.naturalWidth)).toBeGreaterThan(0);
  await expect.poll(() => detail(page).getByLabel("Alarm clip").evaluate(video => video.readyState)).toBeGreaterThanOrEqual(1);
  await expect(detail(page).getByRole("button", { name: "Delete alarm…" })).toHaveCount(0);

  await detail(page).getByRole("button", { name: "Acknowledge" }).click();
  await expect(detail(page)).toContainText("Acknowledged");
  await expect(detail(admin)).toContainText("Acknowledged by camera-operator", { timeout: 10_000 });

  await detail(page).getByLabel("Assign to").selectOption({ label: "camera-operator" });
  await detail(page).getByRole("button", { name: "Assign", exact: true }).click();
  await expect(detail(admin)).toContainText("Assigned to camera-operator", { timeout: 10_000 });

  await detail(page).getByRole("textbox", { name: "Comment" }).fill("Delivery van at the gate");
  await detail(page).getByRole("button", { name: "Add comment" }).click();
  await expect(detail(admin).getByRole("region", { name: "Comments" })).toContainText("Delivery van at the gate", { timeout: 10_000 });

  await detail(page).getByLabel("Resolution note (optional)").fill("Expected delivery");
  await detail(page).getByRole("button", { name: "Resolve" }).click();
  await expect(detail(page)).toContainText("Resolved by camera-operator");
  await expect(detail(admin)).toContainText("Resolved by camera-operator", { timeout: 10_000 });
  await admin.keyboard.press("Escape");
  await expect(alarmChip(admin)).toHaveText("No open alarms", { timeout: 20_000 });
  await admin.close();
});

test("Open in Playback lands on the alarm time with the camera selected", async ({ page }) => {
  const fixtures = seedFixtures(3, { alarm: true });
  await signInNext(page);
  await openAlarm(page);
  await detail(page).getByRole("button", { name: "Open in Playback" }).click();
  await expect(page).toHaveURL(/\/playback$/);
  await expect(inbox(page)).toHaveCount(0);
  const cameraTile = page.getByRole("group", { name: /^Camera 001.*: playback$/ });
  await expect(cameraTile).toBeVisible();
  await expect(cameraTile).toHaveAttribute("data-active", "true");
  await expect.poll(async () => Number(await page.getByLabel("Playback time").getAttribute("data-time"))).toBe(Date.parse(fixtures.alarm.occurred_at));
});

test("only administrators delete alarms, and the alarm footage period is a setting", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page);
  await page.goto("/settings/recording");
  const footage = page.getByRole("form", { name: "Alarm footage" });
  await footage.getByLabel("Days to keep after resolution").fill("45");
  await footage.getByRole("button", { name: "Save" }).click();
  await expect(footage.getByRole("status")).toHaveText("Saved.");
  expect((await (await page.request.get("/api/alarm-settings")).json()).resolved_footage_days).toBe(45);

  await openAlarm(page);
  await detail(page).getByRole("button", { name: "Delete alarm…" }).click();
  await page.getByRole("dialog", { name: "Delete this alarm?" }).getByRole("button", { name: "Delete alarm" }).click();
  await expect(inbox(page).getByRole("list", { name: "Alarms" })).toBeVisible();
  await expect(inbox(page)).toContainText("No alarms match these filters.");
});
