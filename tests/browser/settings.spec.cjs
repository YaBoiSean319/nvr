// Real-browser journeys for the Settings workspace (#103), driven from the keyboard.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

// A valid 1×1 PNG.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");


/** Activate a control the way a keyboard user would. */
async function press(locator, key = "Enter") {
  await locator.focus();
  await locator.press(key);
}

async function openSettings(page, category) {
  await press(page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }));
  await press(page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: category }));
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
  await press(page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Diagnostics" }));
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
  await other.goto("/next/");
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

test("Recording & storage: volume health, backup status and an on-demand backup", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Recording & storage");
  await expect(page.getByRole("region", { name: "Storage volumes" })).toContainText("archive");
  const backups = page.getByRole("region", { name: "Configuration backups" });
  await expect(backups).toContainText("No backup has completed yet.");
  await press(backups.getByRole("button", { name: "Back up now" }));
  await expect(backups.getByRole("status")).toContainText("Backup finished: succeeded", { timeout: 20_000 });
  await expect(backups).toContainText(/Last backup .* \(nvr-config-/);

  // Without a passphrase the reason is shown and nothing can be started.
  await page.route("**/api/backups", async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, enabled: false, disabled_reason: "Set NVR_BACKUP_PASSPHRASE to enable encrypted configuration backups." } });
  });
  await page.reload();
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

test("Integrations and Diagnostics save for real", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openSettings(page, "Integrations");
  const runtime = page.getByRole("form", { name: "AI runtime" });
  await runtime.getByLabel("Cameras analysed at once").fill("6");
  await press(runtime.getByRole("button", { name: "Validate" }));
  await expect(runtime.locator(".field-actions").getByRole("status").or(runtime.locator(".field-actions").getByRole("alert"))).toBeVisible({ timeout: 20_000 });
  await press(runtime.getByRole("button", { name: "Save" }));
  await expect(runtime.getByRole("status").last()).toHaveText("Saved.");
  expect((await (await page.request.get("/api/ai/settings")).json()).max_active_cameras).toBe(6);

  await press(page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Diagnostics" }));
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
  await page.goto("/next/settings/access");
  await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  expect((await page.request.put("/api/settings/display", { data: { time_zone: "UTC", date_format: "iso", clock_format: "24" } })).status()).toBe(403);
  expect((await page.request.post("/api/admin/api-tokens", { data: { name: "x", scopes: ["read"] } })).status()).toBe(403);
  expect((await page.request.post("/api/backups")).status()).toBe(403);
});
