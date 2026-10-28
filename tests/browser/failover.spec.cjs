// Real-browser journeys for the failover recorder (#143): setting an NVR up as a standby, what the status area says, and how a takeover shows. The two-NVR behaviour
// (lease, takeover, handback, footage import) is exercised with real processes in tests/test_failover.py.
const { test, expect, seedFixtures, seedFailover, signInNext } = require("./helpers.cjs");

const settings = page => page.getByRole("region", { name: "Failover settings" });

test("an administrator sets this NVR up as a standby and the status area says so", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/failover");
  await expect(page.getByRole("region", { name: "Failover state" })).toContainText("Failover is off");

  const form = settings(page).getByRole("form", { name: "Failover" });
  await form.getByLabel("This NVR is").selectOption("standby");
  await form.getByLabel("Address of the primary").fill("https://192.168.50.20");
  await form.getByLabel(/^Failover secret/).fill("short");
  await form.getByRole("checkbox", { name: /private network address/ }).check();
  await form.getByRole("button", { name: "Save failover settings" }).click();
  await expect(page.getByRole("alert")).toContainText("at least 16 characters");

  await form.getByLabel(/^Failover secret/).fill("a-failover-secret-of-length");
  await form.getByLabel("Silence before the standby takes over (seconds)").fill("8");
  await form.getByRole("button", { name: "Save failover settings" }).click();
  await expect(page.getByRole("alert")).toContainText("Input should be greater than or equal to 10");
  await form.getByLabel("Silence before the standby takes over (seconds)").fill("100");
  await form.getByLabel("Lease (seconds)").fill("60");
  await form.getByRole("button", { name: "Save failover settings" }).click();
  await expect(page.getByRole("alert")).toContainText("at least twice the lease");

  await form.getByLabel("Silence before the standby takes over (seconds)").fill("120");
  await form.getByRole("button", { name: "Save failover settings" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Failover state" })).toContainText("Standby: watching the primary");
  await expect(page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: "Failover: standby" })).toBeVisible();

  // The secret is never shown again: the form says one is saved and keeps it when the field is left empty.
  await page.reload();
  const again = settings(page).getByRole("form", { name: "Failover" });
  await expect(again.getByLabel(/^Failover secret/)).toHaveValue("");
  await expect(again.getByLabel(/^Failover secret/)).toHaveAttribute("placeholder", "Leave empty to keep it");
  await expect(again.getByLabel("Address of the primary")).toHaveValue("https://192.168.50.20");
  await expect(page.getByRole("button", { name: "Copy the primary's configuration now" })).toBeEnabled();
  await page.getByRole("button", { name: "Copy the primary's configuration now" }).click();
  await expect(page.getByText("The configuration will be copied from the primary in a moment.")).toBeVisible();
});

test("a takeover shows in the status area, the failover page and the incidents", async ({ page }) => {
  seedFixtures(2);
  seedFailover("takeover");
  await signInNext(page);
  const chip = page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: "Failover: standby recording" });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole("heading", { name: "Failover" })).toBeVisible();
  await expect(page.getByText("Taken over: this standby is recording in the primary's place")).toBeVisible();

  await page.goto("/settings/failover");
  await expect(page.getByRole("region", { name: "Failover state" })).toContainText("Taken over");
  await expect(page.getByRole("list", { name: "Takeover history" })).toContainText("Standby recording");
  // A standby that is recording cannot be switched off from here until it has handed back.
  const form = settings(page).getByRole("form", { name: "Failover" });
  await form.getByLabel("This NVR is").selectOption("off");
  await form.getByRole("button", { name: "Save failover settings" }).click();
  await expect(page.getByRole("alert")).toContainText("recording in the primary's place");
});
