// Real-browser journeys for the Settings workspace (#103), driven from the keyboard.
const { test, expect, seedFixtures, signInNext, settingsLink } = require("./helpers.cjs");

// A valid 1×1 PNG.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");


/** Activate a control the way a keyboard user would. */
async function press(locator, key = "Enter") {
  await locator.focus();
  await locator.press(key);
}

async function openSettings(page, category) {
  await press(page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }));
  await press(await settingsLink(page, category));
  await expect(page.getByRole("heading", { level: 1, name: category })).toBeVisible();
}

test("General: a server-rendered preview, an explicit save and a guarded leave", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "General");
  const form = page.getByRole("form", { name: "Date and time display" });
  await form.getByLabel("Time zone").fill("America/New_York");
  await form.getByLabel("Date format").focus();
  await form.getByLabel("Date format").selectOption("iso");
  await press(form.getByLabel("24-hour"), "Space");
  await expect(page.getByTestId("display-preview")).toHaveText("2026-10-21 12:05:06");

  // Leaving with unsaved changes asks first; Cancel stays.
  await press(await settingsLink(page, "Diagnostics"));
  const guard = page.getByRole("dialog", { name: "Unsaved changes" });
  await press(guard.getByRole("button", { name: "Cancel" }));
  await expect(form.getByLabel("Time zone")).toHaveValue("America/New_York");
  await press(form.getByRole("button", { name: "Save" }));
  await expect(form.getByRole("status")).toContainText("Saved.");
  await page.reload();
  await expect(form.getByLabel("Time zone")).toHaveValue("America/New_York");
  await expect(page.getByRole("region", { name: "System status" })).toContainText("Version");
});

test("Appearance: the draft preview is private until saved, uploads are validated per field, failures keep the published branding", async ({ page, browser }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Appearance & branding");
  const form = page.getByRole("form", { name: "Appearance and branding" });
  await form.getByLabel("Application name").fill("North Hall");
  await form.getByLabel("Accent colour hex").fill("#d04a02");
  await expect(form.getByRole("img", { name: "Branding preview (not saved)" })).toContainText("North Hall");

  // Another person sees nothing of the draft.
  const other = await (await browser.newContext()).newPage();
  await other.goto("/");
  await expect(other.getByText("Workplace NVR").first()).toBeVisible();
  await expect(other.getByText("North Hall")).toHaveCount(0);

  // A disguised or oversized file is refused on its own field.
  await form.getByLabel("Upload Icon").setInputFiles({ name: "icon.png", mimeType: "image/png", buffer: Buffer.from("<svg onload=alert(1)></svg>") });
  await expect(form.getByRole("group", { name: "Icon" }).getByRole("alert")).toHaveText("This file is not a readable image.");
  await form.getByLabel("Upload Dark-theme logo").setInputFiles({ name: "big.png", mimeType: "image/png", buffer: Buffer.alloc(2 * 1024 * 1024 + 1) });
  await expect(form.getByRole("group", { name: "Dark-theme logo" }).getByRole("alert")).toHaveText("Each image must be no larger than 2 MiB.");
  await form.getByLabel("Upload Primary logo").setInputFiles({ name: "logo.png", mimeType: "image/png", buffer: PNG });
  await expect(form.getByRole("img", { name: "Primary logo preview" })).toBeVisible();

  // A failed save leaves what everyone sees unchanged.
  await page.route("**/api/settings/branding", route => (route.request().method() === "PUT"
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated outage" }) }) : route.continue()));
  await press(form.getByRole("button", { name: "Save" }));
  await expect(form.getByRole("alert").last()).toContainText("the published branding is unchanged");
  expect((await (await page.request.get("/api/public/branding")).json()).app_name).toBe("Workplace NVR");
  await page.unroute("**/api/settings/branding");
  await press(form.getByRole("button", { name: "Save" }));
  await expect(form.getByRole("status")).toContainText("Everyone sees the new branding.");
  const published = await (await page.request.get("/api/public/branding")).json();
  expect([published.app_name, published.accent_color, Object.keys(published.assets)]).toEqual(["North Hall", "#d04a02", ["primary"]]);
  await other.reload();
  await expect(other.getByText("North Hall").first()).toBeVisible();

  await press(form.getByRole("button", { name: "Reset to defaults…" }));
  await press(page.getByRole("dialog", { name: "Reset branding to defaults?" }).getByRole("button", { name: "Reset to defaults" }));
  await expect(form.getByLabel("Application name")).toHaveValue("Workplace NVR");
  expect((await (await page.request.get("/api/public/branding")).json()).assets).toEqual({});
});

test("Users & access: a token's secret is shown once, it works, and revoking it is immediate", async ({ page, playwright }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Users & access");
  await expect(page.getByRole("region", { name: "Single sign-on" })).toContainText("configured with environment variables");
  await expect(page.getByRole("region", { name: "Active sessions" })).toContainText("camera-admin");
  const create = page.getByRole("form", { name: "New API token" });
  await create.getByLabel("Name").fill("Wall display");
  await press(create.getByRole("checkbox", { name: /^media/ }), "Space");
  await press(create.getByRole("button", { name: "Create token" }));
  const dialog = page.getByRole("dialog", { name: "API token created" });
  const secret = await dialog.getByLabel("Secret value").inputValue();
  expect(secret).toMatch(/^nvrt_/);
  await press(dialog.getByRole("button", { name: "I have stored it" }));
  await expect(page.getByText(secret)).toHaveCount(0);

  const outside = await playwright.request.newContext({ baseURL: new URL(page.url()).origin });
  expect((await outside.get("/api/cameras", { headers: { authorization: `Bearer ${secret}` } })).status()).toBe(200);
  const tokens = page.getByRole("table", { name: "Existing API tokens" });
  await press(tokens.getByRole("row", { name: /Wall display/ }).getByRole("button", { name: "Revoke" }));
  await press(page.getByRole("dialog", { name: "Revoke Wall display?" }).getByRole("button", { name: "Revoke token" }));
  await expect(tokens.getByRole("row", { name: /Wall display/ })).toContainText("revoked");
  expect((await outside.get("/api/cameras", { headers: { authorization: `Bearer ${secret}` } })).status()).toBe(401);
  await outside.dispose();
});

test("Users & access: a metrics token scrapes /metrics and nothing else, and a session cannot", async ({ page, playwright }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Users & access");
  const create = page.getByRole("form", { name: "New API token" });
  await create.getByLabel("Name").fill("Prometheus");
  await create.getByRole("checkbox", { name: /^read/ }).uncheck();
  await create.getByRole("checkbox", { name: /^metrics/ }).check();
  await press(create.getByRole("button", { name: "Create token" }));
  const dialog = page.getByRole("dialog", { name: "API token created" });
  const secret = await dialog.getByLabel("Secret value").inputValue();
  await press(dialog.getByRole("button", { name: "I have stored it" }));
  await expect(page.getByRole("table", { name: "Existing API tokens" }).getByRole("row", { name: /Prometheus/ })).toContainText("metrics");

  const outside = await playwright.request.newContext({ baseURL: new URL(page.url()).origin });
  const scrape = await outside.get("/metrics", { headers: { authorization: `Bearer ${secret}` } });
  expect(scrape.status()).toBe(200);
  const text = await scrape.text();
  expect(text).toContain("nvr_camera_recording_state{");
  expect(text).toContain("nvr_worker_up{");
  expect(text).not.toMatch(/rtsp:|@/);
  expect((await outside.get("/api/cameras", { headers: { authorization: `Bearer ${secret}` } })).status()).toBe(403);
  expect((await page.request.get("/metrics")).status()).toBe(403); // The administrator's own session is not a scraper.
  await outside.dispose();
});

test("live video start-up: the choice is saved, audited and kept", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await openSettings(page, "General");
  const form = page.getByRole("form", { name: "Live video" });
  await expect(form.getByRole("radio", { name: /Ready for every camera/ })).toBeChecked();  // The default: frictionless
  await form.getByRole("radio", { name: /Only while someone is watching/ }).check();
  await expect(form.getByRole("button", { name: "Save" })).toBeEnabled();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved.");
  expect((await (await page.request.get("/api/live-ready")).json()).mode).toBe("off");
  await page.reload();
  await expect(page.getByRole("form", { name: "Live video" }).getByRole("radio", { name: /Only while someone is watching/ })).toBeChecked();
});

test("going back to Settings from a category keeps the groups you had open", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Recording & storage");
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings\/general$/);
  await expect(page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Recording & storage" })).toBeVisible();
});

test("Recording & storage shows one section at a time, chosen from tabs", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Recording & storage");
  await expect(page.getByRole("tab", { name: "Volumes" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "Storage volumes" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Configuration backups" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Backups" }).click();
  await expect(page.getByRole("region", { name: "Configuration backups" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Storage volumes" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Backups" }).press("ArrowLeft");
  await expect(page.getByRole("tab", { name: "Retention" })).toBeFocused();
});

test("Recording & storage: volume health, backup status and an on-demand backup", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Recording & storage");
  await expect(page.getByRole("region", { name: "Storage volumes" })).toContainText("archive");
  await page.getByRole("tab", { name: "Backups" }).click();
  const backups = page.getByRole("region", { name: "Configuration backups" });
  // A fresh fixture has no backup run today, so the background worker may already have taken
  // the day's scheduled catch-up backup (it checks every 30 seconds); either state is correct.
  await expect(backups).toContainText(/No backup has completed yet\.|Last backup .* \(nvr-config-\S+-scheduled\.nvrbackup/);
  await expect(backups).not.toContainText("-manual.nvrbackup");
  await press(backups.getByRole("button", { name: "Back up now" }));
  await expect(backups.getByRole("status")).toContainText("Backup finished: succeeded", { timeout: 20_000 });
  await expect(backups).toContainText(/Last backup .* \(nvr-config-/);
  await expect(backups).toContainText(/nvr-config-\S+-manual\.nvrbackup/);

  // Without a passphrase the reason is shown and nothing can be started.
  await page.route("**/api/backups", async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, enabled: false, disabled_reason: "Set NVR_BACKUP_PASSPHRASE to enable encrypted configuration backups." } });
  });
  await page.reload();
  await page.getByRole("tab", { name: "Backups" }).click();
  await expect(backups).toContainText("Backups are disabled: Set NVR_BACKUP_PASSPHRASE");
  await expect(backups.getByRole("button", { name: "Back up now" })).toBeDisabled();
});

test("Notifications: webhooks with secrets shown once, a test delivery, and a heartbeat", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Notifications");
  const form = page.getByRole("form", { name: "New webhook" });
  await form.getByLabel("Name").fill("Pager");
  await form.getByLabel("Receiver URL").fill("http://10.255.255.1:9/hook");
  await press(form.getByRole("checkbox", { name: "recording.failure" }), "Space");
  await press(form.getByLabel("Allow a receiver on a private network"), "Space");
  await press(form.getByRole("button", { name: "Add webhook" }));
  const secret = page.getByRole("dialog", { name: "Signing secret for Pager" });
  await expect(secret.getByLabel("Secret value")).not.toHaveValue("");
  await press(secret.getByRole("button", { name: "I have stored it" }));
  const hook = page.getByRole("listitem", { name: "Webhook Pager" });
  await press(hook.getByRole("button", { name: "Send test" }));
  await expect(page.getByRole("region", { name: "Delivery history" }).getByRole("row", { name: /Pager/ })).toBeVisible({ timeout: 15_000 });
  await press(hook.getByRole("button", { name: "Pause" }));
  await expect(hook).toContainText("paused");

  const heartbeat = page.getByRole("form", { name: "Heartbeat" });
  await heartbeat.getByLabel("Receiver URL").fill("http://10.255.255.1:9/heartbeat");
  await press(heartbeat.getByLabel("Allow a receiver on a private network"), "Space");
  await press(heartbeat.getByRole("button", { name: "Save" }));
  await press(page.getByRole("dialog", { name: "Heartbeat signing secret" }).getByRole("button", { name: "I have stored it" }));
  await expect(heartbeat).toContainText("Sending to http://10.255.255.1:9 every 60 s");
  await press(heartbeat.getByRole("button", { name: "Remove heartbeat" }));
  await expect(heartbeat).toContainText("Not configured");
});

test("AI analysis and Diagnostics save for real", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "AI analysis");
  const runtime = page.getByRole("form", { name: "AI runtime" });
  // Automatic is the default, with no number to set; a fixed number can be any whole number, not only up to sixteen.
  await expect(runtime.getByRole("radio", { name: /^Automatic/ })).toBeChecked();
  await expect(runtime.getByLabel("Cameras analysed at once")).toHaveCount(0);
  await expect(runtime.getByText("The AI worker has not reported yet.").or(runtime.getByRole("status", { name: "AI capacity now" }))).toBeVisible();
  await runtime.getByRole("radio", { name: "A fixed number" }).check();
  await runtime.getByLabel("Cameras analysed at once").fill("40");
  await press(runtime.getByRole("button", { name: "Save" }));
  await expect(runtime.getByRole("status").last()).toHaveText("Saved.");
  expect(await (await page.request.get("/api/ai/settings")).json()).toMatchObject({ capacity_mode: "fixed", max_active_cameras: 40 });
  await runtime.getByLabel("Cameras analysed at once").fill("6");
  await press(runtime.getByRole("button", { name: "Validate" }));
  await expect(runtime.locator(".field-actions").getByRole("status").or(runtime.locator(".field-actions").getByRole("alert"))).toBeVisible({ timeout: 20_000 });
  await press(runtime.getByRole("button", { name: "Save" }));
  await expect(runtime.getByRole("status").last()).toHaveText("Saved.");
  expect((await (await page.request.get("/api/ai/settings")).json()).max_active_cameras).toBe(6);

  await press(await settingsLink(page, "Diagnostics"));
  const diagnostics = page.getByRole("region", { name: "Support diagnostics" });
  await diagnostics.getByLabel("Capture for").fill("2");
  await press(diagnostics.getByRole("button", { name: "Start capturing" }));
  await expect(diagnostics.getByRole("status")).toContainText("Capturing until");
  const download = page.waitForEvent("download");
  await press(diagnostics.getByRole("link", { name: "Download support bundle" }));
  expect((await download).suggestedFilename()).toBe("nvr-diagnostics.json");
  await press(diagnostics.getByRole("button", { name: "Stop capturing" }));
  await expect(diagnostics.getByRole("status")).toContainText("Not capturing.");
});

test("operators cannot reach Settings and the server refuses their changes", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page, "camera-operator");
  await expect(page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" })).toHaveCount(0);
  await page.goto("/settings/access");
  await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  expect((await page.request.put("/api/settings/display", { data: { time_zone: "UTC", date_format: "iso", clock_format: "24" } })).status()).toBe(403);
  expect((await page.request.post("/api/admin/api-tokens", { data: { name: "x", scopes: ["read"] } })).status()).toBe(403);
  expect((await page.request.post("/api/backups")).status()).toBe(403);
});

test("Coverage & self-test: a self-test run on demand, and coverage per camera with its largest gaps and their reasons", async ({ page }) => {
  seedFixtures(1, { footage: true });
  await signInNext(page);
  await openSettings(page, "Coverage & self-test");
  const selfTest = page.getByRole("region", { name: "Self-test" });
  await press(selfTest.getByRole("button", { name: "Run self-test now" }));
  await expect(selfTest.getByRole("status")).toContainText(/run by hand/);
  const checks = selfTest.getByRole("list", { name: "Self-test checks" });
  // The fixture's archive volume folder does not exist: that check fails, with its reason.
  await expect(checks.getByRole("listitem")).toContainText(["Passed · Recording volume default is writable",
    "Failed · Recording volume archive is not writable (missing)", /Configuration backup|configuration backup/]);
  await expect(selfTest.getByRole("status")).toContainText("Failed 1 check(s)");

  const report = page.getByRole("region", { name: "Coverage report" });
  const row = report.getByRole("table", { name: "Coverage per camera" }).getByRole("row", { name: /Camera 001/ });
  await expect(row).toContainText("%");
  await expect(row).toContainText("Camera offline");

  await openSettings(page, "General");
  await expect(page.getByTestId("self-test-summary")).toContainText(/Self-test (passed|failed)/);
});

test("Remote access: how the page was reached, no warnings on the local network, and a second factor for remote sessions", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Remote access");
  const connected = page.getByRole("region", { name: "How you are connected" });
  await expect(connected).toContainText("127.0.0.1 · the local network");
  await expect(connected).toContainText("Plain HTTP");
  await expect(connected).toContainText("None. Set NVR_TRUSTED_PROXIES");
  // Opened on the local network, nothing here is exposed: there is nothing to warn about.
  await expect(page.getByRole("region", { name: "Warnings" }).getByRole("status")).toContainText("Nothing to warn about");

  const remote = page.getByRole("checkbox", { name: /Require a second factor for sessions from outside the local network/ });
  await expect(remote).not.toBeChecked();
  await press(remote, "Space");
  await expect(remote).toBeChecked();
  await page.reload();
  await expect(page.getByRole("checkbox", { name: /Require a second factor for sessions from outside the local network/ })).toBeChecked();
  const saved = await (await page.request.get("/api/admin/security")).json();
  expect(saved).toEqual({ mfa_policy: "off", remote_mfa: true });
  // Turning it on while connected from the local network does not ask this session for anything.
  await expect(page.getByRole("heading", { level: 1, name: "Remote access" })).toBeVisible();
});

test("categories are grouped under headings, only the open group lists its links, and every category is two clicks away", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/settings/general");
  const nav = page.getByRole("navigation", { name: "Settings categories" });
  await expect(nav.locator("summary")).toHaveText(["Site", "People & access", "Recording & storage", "Alerts", "AI", "System"]);
  // Only the group of the page being looked at is open.
  await expect(nav.getByRole("link", { name: "Appearance & branding" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Camera hardening" })).toBeHidden();
  // One click opens a group, a second goes to the category, and the group stays open.
  await nav.locator("summary", { hasText: "People & access" }).click();
  await expect(nav.getByRole("link", { name: "Camera hardening" })).toBeVisible();
  await nav.getByRole("link", { name: "Camera hardening" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Camera hardening" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Camera hardening" })).toBeVisible();
  // Every one of the 22 categories is in exactly one group.
  while (await nav.locator("details:not([open])").count()) await nav.locator("details:not([open]) summary").first().click(); // Each click removes one from the list
  await expect(nav.getByRole("link")).toHaveCount(22);
  // The old address of the AI settings still works, under its new name.
  await page.goto("/settings/integrations");
  await expect(page.getByRole("heading", { level: 1, name: "AI analysis" })).toBeVisible();
});

test("a setting can be found by typing what it is about, and Enter opens the first match", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/settings/general");
  const find = page.getByRole("searchbox", { name: "Find a setting" });
  const nav = page.getByRole("navigation", { name: "Settings categories" });
  await find.fill("microsoft");
  await expect(nav.getByRole("link")).toHaveText(["Email alerts"]); // Found by a word that is not in its name
  await find.fill("zzzz");
  await expect(nav.getByRole("status")).toContainText("No setting matches");
  await find.fill("ai model");
  await expect(nav.getByRole("link")).toHaveText(["AI analysis"]);
  await find.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "AI analysis" })).toBeVisible();
  await expect(find).toHaveValue(""); // Cleared once it has taken you there
});

test("on a phone the categories are one picker, not a strip to scroll", async ({ page }) => {
  seedFixtures(1);
  await page.setViewportSize({ width: 390, height: 800 });
  await signInNext(page);
  await page.goto("/settings/general");
  const picker = page.getByRole("combobox", { name: "Settings category" });
  await expect(picker).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Settings categories" })).toBeHidden();
  expect((await picker.boundingBox()).y).toBeLessThan(250); // Near the top, no scrolling past a list to reach the page
  await picker.selectOption("tiering");
  await expect(page.getByRole("heading", { level: 1, name: "Storage tiering" })).toBeVisible();
  // Typing finds a category here too.
  await page.getByRole("searchbox", { name: "Find a setting" }).fill("email");
  await expect(page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Email alerts" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
