// Rule actions (#113): an administrator adds actions to a rule; their secrets never come back,
// test mode lists them without running them, and the rule shows its delivery log.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const SLACK_URL = "https://8.8.8.8/services/T000/B000/browser-secret-token";
const MQTT_URL = "mqtts://bus:browser-broker-pass@8.8.8.8:8883";

test("an administrator adds actions whose secrets are never shown again, and test mode runs none of them", async ({ page }) => {
  seedFixtures(2, { detections: true });
  await signInNext(page);
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await page.getByRole("region", { name: "Rules" }).getByRole("button", { name: "New rule" }).click();

  const editor = page.getByRole("region", { name: /^Rule: / });
  const form = editor.getByRole("form", { name: "Rule settings" });
  await form.getByLabel("Name").fill("Gate entry");
  await form.getByRole("checkbox", { name: "Camera 001" }).check();
  await form.getByRole("checkbox", { name: "Vehicles" }).check();

  await form.getByLabel("Add an action").selectOption({ label: "Slack message" });
  const slack = form.getByRole("region", { name: "Action: Slack message" });
  await slack.getByLabel("Name", { exact: true }).fill("Slack #security");
  await form.getByRole("region", { name: "Action: Slack #security" }).getByLabel("Incoming webhook URL").fill(SLACK_URL);
  await form.getByLabel("Add an action").selectOption({ label: "MQTT message" });
  const bus = form.getByRole("region", { name: "Action: MQTT message" });
  await bus.getByLabel("Broker").fill(MQTT_URL);
  await expect(form.getByRole("button", { name: "Save" })).toBeDisabled();
  await bus.getByLabel("Topic").fill("site/gate");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved.");

  // The saved actions show where they deliver, never the secret parts.
  await expect(form.getByRole("region", { name: "Action: Slack #security" })).toContainText("https://8.8.8.8");
  await expect(form.getByRole("region", { name: "Action: Slack #security" }).getByLabel("Incoming webhook URL")).toHaveValue("");
  await expect(form.getByRole("region", { name: "Action: Slack #security" }).getByLabel("Incoming webhook URL")).toHaveAttribute("placeholder", "Saved — leave empty to keep");
  await expect(form.getByRole("region", { name: "Action: MQTT message" })).toContainText("mqtts://8.8.8.8:8883");
  const listed = await (await page.request.get("/api/rules")).text();
  for (const secret of ["browser-secret-token", "browser-broker-pass"]) {
    expect(listed).not.toContain(secret);
    expect(await page.content()).not.toContain(secret);
  }

  // Test mode lists the actions it would run and runs none.
  const testing = page.getByRole("region", { name: "Test mode" });
  await testing.getByRole("button", { name: "Test against the last 24 hours" }).click();
  await expect(testing.getByRole("status")).toContainText("test mode ran none of them: Slack #security, MQTT message");
  const deliveries = page.getByRole("region", { name: "Action deliveries" });
  await expect(deliveries).toContainText("Nothing delivered yet.");

  // Saving again without retyping the secrets keeps them.
  await form.getByRole("region", { name: "Action: MQTT message" }).getByLabel("Name", { exact: true }).fill("Building bus");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved.");
  const rule = (await (await page.request.get("/api/rules")).json()).rules.find(item => item.name === "Gate entry");
  expect(rule.actions.map(action => [action.name, action.target])).toEqual([["Slack #security", "https://8.8.8.8"], ["Building bus", "mqtts://8.8.8.8:8883"]]);
});
