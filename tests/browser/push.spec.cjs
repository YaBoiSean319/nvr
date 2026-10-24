// Push notifications for alarms (#111): the service worker shows a pushed alarm and its click opens
// that alarm; each person chooses what notifies them.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

// Chromium's headless shell never shows notifications; full Chromium in headless mode does.
test.use({ channel: "chromium" });

async function controlledRegistration(page) {
  await page.evaluate(() => navigator.serviceWorker.ready);
  if (!(await page.evaluate(() => Boolean(navigator.serviceWorker.controller)))) await page.reload();
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);
}

test("a pushed alarm shows a notification whose click opens that alarm", async ({ page, context, baseURL }) => {
  const fixtures = seedFixtures(2, { alarm: true });
  const alarmId = fixtures.alarm.alarm_id;
  await context.grantPermissions(["notifications"], { origin: baseURL });
  await signInNext(page);
  await controlledRegistration(page);

  // Deliver a push to the installed worker the way a push service would.
  const session = await context.newCDPSession(page);
  const registrations = [];
  const versions = [];
  session.on("ServiceWorker.workerRegistrationUpdated", event => registrations.push(...event.registrations));
  session.on("ServiceWorker.workerVersionUpdated", event => versions.push(...event.versions));
  await session.send("ServiceWorker.enable");
  await expect.poll(() => registrations.find(item => item.scopeURL === `${baseURL}/` && !item.isDeleted)).toBeTruthy();
  const registrationId = registrations.find(item => item.scopeURL === `${baseURL}/` && !item.isDeleted).registrationId;
  await expect.poll(() => versions.some(version => version.registrationId === registrationId && version.status === "activated")).toBe(true);
  const message = { alarm_id: alarmId, title: "Gate entry: Camera 001", body: "Vehicle at 10:00:00", url: `/?alarm=${alarmId}`, thumbnail_url: "/api/push/thumbnails/x" };
  const shown = () => page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready;
    return (await registration.getNotifications()).map(item => ({ title: item.title, body: item.body, tag: item.tag, url: item.data && item.data.url }));
  });
  // A push delivered while the browser is still starting the worker can be dropped, as a push service
  // may redeliver; a redelivery replaces the notification (same tag), so at most one is ever shown.
  for (let attempt = 0; attempt < 3 && !(await shown()).length; attempt += 1) {
    await session.send("ServiceWorker.deliverPushMessage", { origin: baseURL, registrationId, data: JSON.stringify(message) });
    await expect.poll(async () => (await shown()).length, { timeout: 4_000 }).toBe(1).catch(() => undefined);
  }
  expect(await shown()).toEqual([{ title: "Gate entry: Camera 001", body: "Vehicle at 10:00:00", tag: `alarm-${alarmId}`, url: `/?alarm=${alarmId}` }]);

  // Clicking the notification opens its address; that address opens the alarm and tidies itself.
  await page.goto(`/?alarm=${alarmId}`);
  const detail = page.getByRole("dialog", { name: "Alarms" }).getByRole("article", { name: "Alarm: Gate entry" });
  await expect(detail).toBeVisible();
  await expect(page).not.toHaveURL(/alarm=/);
});

test("each person chooses the rules that notify them and their quiet hours", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page, "camera-operator");
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i }).click();
  const inbox = page.getByRole("dialog", { name: "Alarms" });
  await inbox.getByText("Notification settings").click();
  const form = inbox.getByRole("form", { name: "Notification choices" });
  await form.getByRole("checkbox", { name: "Gate entry" }).uncheck();
  await form.getByRole("checkbox", { name: "Quiet hours" }).check();
  await form.getByLabel("From").fill("21:30");
  await form.getByLabel("To").fill("06:45");
  await form.getByRole("button", { name: "Save notification choices" }).click();
  await expect(inbox.getByRole("status").filter({ hasText: "Notification choices saved." })).toBeVisible();

  const saved = await (await page.request.get("/api/push/preferences")).json();
  expect(saved.rule_ids).toEqual([]);
  expect(saved.quiet_hours).toEqual({ start: "21:30", end: "06:45", timezone: "UTC" });
  await expect(form.getByRole("button", { name: "Save notification choices" })).toBeDisabled();
});
