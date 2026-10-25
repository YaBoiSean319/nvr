// Real-browser journey for the camera hardening report (#124).
const { test, expect, seedFixtures, signInNext, weakenCamera } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const STRONG = "Zq9-long-unique-passphrase";

async function openHardening(page) {
  await nav(page).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Camera hardening" }).click();
}

test("the hardening report finds each weakness, takes an acknowledgement with a reason, and shows the finding again when its condition changes", async ({ page }) => {
  const fixtures = seedFixtures(3, {
    streams: {
      1: "rtsp://admin:admin@192.0.2.1:8554/nvr-test", // A factory default login, on the camera network.
      2: `rtsp://viewer:${STRONG}@198.51.100.7:8554/nvr-test`, // A chosen login, off the camera network.
      3: `rtsp://viewer:${STRONG}@192.0.2.3:8554/nvr-test`, // Clean.
    },
  });
  await signInNext(page);
  await openHardening(page);
  await expect(page.getByRole("region", { name: "Hardening checks" })).toContainText("The checks have not run yet");
  await expect(page.getByRole("region", { name: "Findings" })).toContainText("Nothing to show until the checks have run");

  // Name the camera network and stop probing cameras (the fixture addresses answer nothing); saving runs the checks at once.
  const networks = page.getByRole("region", { name: "Camera networks and probes" });
  await networks.getByRole("textbox", { name: "Camera networks (one per line)" }).fill("192.0.2.0/24");
  await networks.getByRole("checkbox", { name: /Ask cameras whether they offer RTSPS and HTTPS/ }).uncheck();
  await networks.getByRole("button", { name: "Save" }).click();

  const table = page.getByRole("table", { name: "Hardening findings" });
  await expect(table.getByRole("row", { name: /Camera 001/ })).toContainText("Uses a well-known default password");
  await expect(table.getByRole("row", { name: /Camera 001/ })).toContainText("High");
  await expect(table.getByRole("row", { name: /Camera 002/ })).toContainText("Answers on an address outside the camera network");
  await expect(table.getByRole("row", { name: /Camera 002/ })).toContainText("198.51.100.7");
  await expect(table.getByRole("row", { name: /Camera 003/ })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Hardening checks" })).toContainText("1 high and 1 medium");
  // The login that matched the list is not shown anywhere.
  await expect(page.locator("body")).not.toContainText("admin:admin");
  await expect(page.locator("body")).not.toContainText(STRONG);

  // An acknowledgement needs a reason, then hides the finding and keeps the reason.
  await page.getByRole("button", { name: "Acknowledge Uses a well-known default password on Camera 001" }).click();
  const confirm = table.getByRole("button", { name: "Acknowledge", exact: true });
  await expect(confirm).toBeDisabled();
  await table.getByRole("textbox", { name: /Why is this accepted/ }).fill("Bench unit with no route to the network");
  await confirm.click();
  await expect(table.getByRole("row", { name: /Camera 001/ })).toHaveCount(0);
  const hidden = page.getByRole("region", { name: "Acknowledged findings" });
  await expect(hidden).toContainText("Camera 001");
  await expect(hidden).toContainText("Bench unit with no route to the network");

  // Running again does not bring it back: nothing about it changed.
  await page.getByRole("button", { name: "Run checks now" }).click();
  await expect(page.getByRole("region", { name: "Hardening checks" })).toContainText("Run by hand");
  await expect(table.getByRole("row", { name: /Camera 001/ })).toHaveCount(0);
  await expect(hidden).toContainText("Bench unit with no route to the network");

  // The condition changes (the ONVIF login is a default too): a new finding, shown again, and the old acknowledgement is gone.
  weakenCamera(1, { onvifUser: "admin", onvifPassword: "admin" });
  await page.getByRole("button", { name: "Run checks now" }).click();
  await expect(table.getByRole("row", { name: /Camera 001/ })).toContainText("its stream login and its ONVIF login");
  await expect(page.getByRole("region", { name: "Acknowledged findings" })).toHaveCount(0);

  // Each finding leads to the camera.
  await table.getByRole("button", { name: "Camera 001", exact: true }).click();
  await expect(page).toHaveURL(/\/cameras$/);
  await expect(page.getByRole("region", { name: "Settings for Camera 001" })).toBeVisible(); // The camera the finding was about, and no other.
  await expect(page.getByRole("tab", { name: "Recording" })).toBeVisible();
  const report = await (await page.request.get("/api/hardening")).json();
  expect(report.findings.map(item => [item.camera_id, item.check]).sort()).toEqual(
    [[fixtures.camera_ids[0], "default_credentials"], [fixtures.camera_ids[1], "outside_network"]].sort());
});
