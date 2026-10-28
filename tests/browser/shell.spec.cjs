// Real-browser journeys for the rebuilt interface's foundation (#89): first-run
// setup, sign-in, role-gated navigation, operational status, branding and theme.
const { test, expect, FIXTURE_PASSWORD, seedFixtures, signInNext } = require("./helpers.cjs");

const NEXT = "/";
const ADMIN = "camera-admin";
const OPERATOR = "camera-operator";

async function expectNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const width of [360, 1440]) {
  test(`first-run setup creates the administrator with the keyboard at ${width}px`, async ({ page }) => {
    seedFixtures(0, { freshInstall: true });
    await page.setViewportSize({ width, height: 800 });
    await page.goto(NEXT);
    await expect(page.getByRole("heading", { name: "Create the administrator account" })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    // Validation is explained before anything is sent.
    await page.keyboard.type("first-admin");
    await page.keyboard.press("Tab");
    await page.keyboard.type("short1");
    await page.keyboard.press("Enter");
    await expect(page.getByText("Use at least 14 characters.")).toBeVisible();

    const password = `${FIXTURE_PASSWORD}9a`;
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").focus();
    await page.keyboard.type(password);
    await page.keyboard.press("Enter");
    await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();
    await expect(page).toHaveTitle(/Live · Workplace NVR/);
    await expectNoHorizontalOverflow(page);

    // The setup endpoint is closed for good once an administrator exists.
    const again = await page.request.post("/api/setup/admin", { data: { username: "second-admin", password } });
    expect(again.status()).toBe(409);
  });
}

test("sign-in reports bad credentials and hides Entra when it is not configured", async ({ page }) => {
  seedFixtures(0);
  await page.goto(NEXT);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Entra/ })).toHaveCount(0);
  await page.getByLabel("Username").fill(ADMIN);
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Invalid username or password");
});

test("administrators see every destination and status opens with the keyboard", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page, ADMIN);
  const nav = page.getByRole("navigation", { name: "Workspaces" });
  await expect(nav.getByRole("link")).toHaveText(["Live", "Playback", "Insights", "Cameras", "Settings"]);
  await expect(nav.getByRole("link", { name: "Live" })).toHaveAttribute("aria-current", "page");

  const recording = page.getByRole("button", { name: /^Recording:/ });
  await expect(recording).toBeVisible();
  await expect(page.getByRole("button", { name: /^Video:/ })).toBeVisible();
  await recording.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "Operational status" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "Recording" })).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "Live video" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(recording).toBeFocused();

  await nav.getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings\/general$/);
  await expect(page).toHaveTitle(/Settings · /);
});

test("operators never see administrator destinations and the server refuses them", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page, OPERATOR);
  const nav = page.getByRole("navigation", { name: "Workspaces" });
  // Operators get read-only camera details (#67) but no configuration destinations.
  await expect(nav.getByRole("link")).toHaveText(["Live", "Playback", "Insights", "Cameras"]);
  await page.goto(`${NEXT}settings`);
  await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  const branding = await page.request.get("/api/settings/branding");
  expect(branding.status()).toBe(403);
  const sessions = await page.request.get("/api/admin/sessions");
  expect(sessions.status()).toBe(403);
});

test("the title keeps its workspace when branding arrives after the shell is showing", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page, ADMIN);
  const saved = await page.request.put("/api/settings/branding", { data: { app_name: "Late Branding Hall", primary_color: "#0f3d2e", accent_color: "#2a6f4e" } });
  expect(saved.ok()).toBeTruthy();
  // Answer the branding request only after the signed-in shell has been drawn, the order that used to drop the workspace from the title.
  await page.route("**/api/public/branding", async route => {
    await new Promise(resolve => setTimeout(resolve, 1500));
    await route.continue();
  });
  await page.reload();
  await expect(page.locator(".shell-app-name")).toHaveText("Late Branding Hall", { timeout: 10_000 });
  await expect(page).toHaveTitle("Live · Late Branding Hall");
});

test("site branding reaches sign-in, the shell and the title, with a legible accent", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page, ADMIN);
  const saved = await page.request.put("/api/settings/branding", {
    data: { app_name: "North Hall Security", primary_color: "#0f3d2e", accent_color: "#f6f064" },
  });
  expect(saved.ok()).toBeTruthy();
  await page.getByRole("button", { name: /Account menu/ }).click();
  await page.getByRole("menuitemradio", { name: "Light" }).click();
  await page.reload();
  await expect(page.locator(".shell-app-name")).toHaveText("North Hall Security");
  await expect(page).toHaveTitle("Live · North Hall Security");

  // A pale yellow accent fails on white; the shell darkens it until text reaches 4.5:1.
  const ratio = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--n-accent)";
    document.body.append(probe);
    const [r, g, b] = getComputedStyle(probe).color.match(/\d+/g).map(Number);
    probe.remove();
    const linear = value => { const v = value / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const lum = 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
    return (1 + 0.05) / (lum + 0.05);
  });
  expect(ratio).toBeGreaterThanOrEqual(4.5);

  await page.getByRole("button", { name: /Account menu/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.locator(".auth-app-name")).toHaveText("North Hall Security");
});

test("theme preference belongs to each user and survives a new browser profile", async ({ page, browser }) => {
  seedFixtures(0);
  await signInNext(page, ADMIN);
  await page.getByRole("button", { name: /Account menu/ }).click();
  await page.getByRole("menuitemradio", { name: "Dark" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  const fresh = await browser.newContext();
  const other = await fresh.newPage();
  await signInNext(other, ADMIN);
  await expect(other.locator("html")).toHaveAttribute("data-theme", "dark");
  await fresh.close();

  const operatorContext = await browser.newContext();
  const operator = await operatorContext.newPage();
  await signInNext(operator, OPERATOR);
  await expect(operator.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await operatorContext.close();
});

test("the shell stays within narrow screens", async ({ page }) => {
  seedFixtures(2);
  await page.setViewportSize({ width: 320, height: 640 });
  await signInNext(page, ADMIN);
  await expectNoHorizontalOverflow(page);
  await expect(page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Account menu/ })).toBeVisible();
});
