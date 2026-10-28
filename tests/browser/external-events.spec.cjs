// Real-browser journey for outside events (#138): an administrator adds a signed source, maps a door to a camera, an event arrives, and the secret is never shown again.
const crypto = require("node:crypto");
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

function signed(secret, body) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto.createHmac("sha256", secret).update(`${timestamp}.${body}`).digest("hex");
  return { "Content-Type": "application/json", "X-NVR-Timestamp": timestamp, "X-NVR-Signature": `sha256=${signature}` };
}

test("an administrator adds a signed source, maps a door, and sees the event that arrives", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/events");
  await page.getByRole("button", { name: "Add an event source" }).click();
  const form = page.getByRole("group", { name: "Event source" });
  await form.getByLabel("Name").fill("Door controller");
  await form.getByRole("button", { name: "Generate" }).click();
  const secret = await form.getByLabel("Signing secret").inputValue();
  expect(secret).toMatch(/^[0-9a-f]{48}$/);
  await form.getByRole("button", { name: "Add a mapping" }).click();
  const mapping = form.getByRole("group", { name: "Mapping 1" });
  await mapping.getByLabel("Subject").fill("main-door");
  await mapping.getByRole("checkbox").first().check();
  await form.getByRole("button", { name: "Save source" }).click();
  await expect(page.getByText("Saved.", { exact: true })).toBeVisible();

  // The secret is not shown again, in the page or in what the API returns.
  await page.reload();
  expect(await page.content()).not.toContain(secret);
  const listed = await (await page.request.get("/api/event-sources")).text();
  expect(listed).not.toContain(secret);
  const source = JSON.parse(listed).sources[0];

  // A signed event is accepted; an unsigned one is not.
  const body = JSON.stringify({ event_id: "evt-1", type: "door_forced", subject: "main-door", message: "Forced open" });
  const accepted = await page.request.post(`/api/inbound/events/${source.id}`, { data: body, headers: signed(secret, body) });
  expect(accepted.status()).toBe(202);
  const unsigned = await page.request.post(`/api/inbound/events/${source.id}`, { data: body, headers: { "Content-Type": "application/json" } });
  expect(unsigned.status()).toBe(401);

  await page.getByRole("button", { name: "Recent events" }).first().click();
  await expect(page.getByRole("list", { name: "Recent events" })).toContainText("main-door");
  await expect(page.getByRole("list", { name: "Recent events" })).toContainText("mapped to a camera");
});
