// The interface as the native app runs it (#140). A stand-in for Capacitor's bridge and plugins is injected, with the app's user agent, so the same web interface is
// exercised the way the app wraps it: biometric lock after the idle time, secure-storage session, native push registration and a tap on a notification.
// What a real phone adds (the OS dialogs, FCM and APNs, the Keychain itself) is listed as post-merge checks in docs/mobile-app.md.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const APP_AGENT = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36 NVRApp/1.0";
test.use({ userAgent: APP_AGENT });

function installBridge() {
  const store = key => ({ get: () => JSON.parse(localStorage.getItem(key) || "{}"), set: value => localStorage.setItem(key, JSON.stringify(value)) });
  const prefs = store("stub.prefs");
  const creds = store("stub.creds");
  const listeners = {};
  const stub = { verify: { ok: !localStorage.getItem("stub.deny"), calls: 0 }, skew: 0, pushToken: "fcm-registration-token:" + "A".repeat(60), emit: (name, data) => (listeners[name] || []).forEach(callback => callback(data)) };
  window.__stub = stub;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + stub.skew;
  const on = (name, callback) => { (listeners[name] = listeners[name] || []).push(callback); return { remove() { listeners[name] = listeners[name].filter(item => item !== callback); } }; };
  window.Capacitor = {
    isNativePlatform: () => true,
    getPlatform: () => "android",
    Plugins: {
      Preferences: {
        get: async ({ key }) => ({ value: prefs.get()[key] ?? null }),
        set: async ({ key, value }) => { const all = prefs.get(); all[key] = value; prefs.set(all); },
        remove: async ({ key }) => { const all = prefs.get(); delete all[key]; prefs.set(all); },
      },
      NativeBiometric: {
        isAvailable: async () => ({ isAvailable: true }),
        verifyIdentity: async () => { stub.verify.calls += 1; if (!stub.verify.ok) throw new Error("cancelled"); },
        setCredentials: async ({ server, username, password }) => { const all = creds.get(); all[server] = { username, password }; creds.set(all); },
        getCredentials: async ({ server }) => { const found = creds.get()[server]; if (!found) throw new Error("no credentials"); return found; },
        deleteCredentials: async ({ server }) => { const all = creds.get(); delete all[server]; creds.set(all); },
      },
      PushNotifications: {
        requestPermissions: async () => ({ receive: "granted" }),
        register: async () => { setTimeout(() => stub.emit("registration", { value: stub.pushToken }), 0); },
        addListener: async (name, callback) => on(name, callback),
      },
      App: { addListener: async (name, callback) => on(name, callback) },
    },
  };
}

test.beforeEach(async ({ page }) => { await page.addInitScript(installBridge); });

const inbox = page => page.getByRole("dialog", { name: "Alarms" });
const alarmChip = page => page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i });

async function openNotificationSettings(page) {
  await alarmChip(page).click();
  await inbox(page).getByText("Notification settings").click();
}

test("everything the app fetches is marked not to be cached, and no worker keeps a shell", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  const responses = [];
  page.on("response", response => responses.push(response));
  await signInNext(page);
  await page.waitForLoadState("networkidle");
  const cached = [];
  for (const response of responses) {
    const url = new URL(response.url());
    if (!/^\/(api\/|assets\/|$)/.test(url.pathname)) continue;
    if (response.headers()["cache-control"] !== "no-store") cached.push(`${url.pathname} ${response.headers()["cache-control"]}`);
  }
  expect(cached).toEqual([]);
  expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
  expect(await page.evaluate(async () => (await caches.keys()).length)).toBe(0);
});

test("the app is locked after the idle time and opens with the phone's biometrics", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page);
  await openNotificationSettings(page);
  const phone = inbox(page).getByRole("group", { name: "This phone" });
  await phone.getByLabel("Lock the app with face, fingerprint or passcode").check();
  await phone.getByLabel("Lock after being idle for").selectOption("1");
  await page.keyboard.press("Escape");

  // Not idle long enough: still open. Idle for more than a minute: locked, and the interface is not on screen.
  await page.evaluate(() => { window.__stub.skew += 30_000; window.__stub.emit("appStateChange", { isActive: true }); });
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
  await page.evaluate(() => { window.__stub.verify.ok = false; window.__stub.skew += 90_000; window.__stub.emit("appStateChange", { isActive: true }); });
  const lock = page.getByRole("dialog", { name: "NVR is locked" });
  await expect(lock).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toHaveCount(0);
  await expect(lock).toContainText("That did not unlock it.");

  await page.evaluate(() => { window.__stub.verify.ok = true; });
  await lock.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
  expect(await page.evaluate(() => window.__stub.verify.calls)).toBeGreaterThanOrEqual(2);
});

test("opening the app after it sat idle asks for the biometrics before anything shows", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page);
  await openNotificationSettings(page);
  const phone = inbox(page).getByRole("group", { name: "This phone" });
  await phone.getByLabel("Lock the app with face, fingerprint or passcode").check();
  await page.keyboard.press("Escape");
  // The app was closed ten minutes ago (the idle time is five by default).
  await page.evaluate(() => {
    const prefs = JSON.parse(localStorage.getItem("stub.prefs"));
    prefs["lock.last_active"] = String(Date.now() - 10 * 60_000);
    localStorage.setItem("stub.prefs", JSON.stringify(prefs));
    localStorage.setItem("stub.deny", "1");  // The phone does not recognise the person
  });
  await page.reload();
  await expect(page.getByRole("dialog", { name: "NVR is locked" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "NVR is locked" })).toContainText("That did not unlock it.");
  await page.evaluate(() => { window.__stub.verify.ok = true; });
  await page.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
});

test("the session is kept in secure storage and signs the person back in after the cookie is gone", async ({ page, context }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page);
  await expect.poll(() => page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem("stub.creds") || "{}")).length)).toBe(1);
  const first = await page.evaluate(() => Object.values(JSON.parse(localStorage.getItem("stub.creds")))[0].password);
  expect(first).toMatch(/^nvrd_/);
  await context.clearCookies();
  await page.reload();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
  const second = await page.evaluate(() => Object.values(JSON.parse(localStorage.getItem("stub.creds")))[0].password);
  expect(second).not.toBe(first);  // Each use replaces the secret
  // Signing out removes it from the phone and from the NVR.
  await page.getByRole("button", { name: /camera-admin/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByLabel("Username")).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("stub.creds"))).toBe("{}");
  await page.reload();
  await expect(page.getByLabel("Username")).toBeVisible();  // The old secret does not sign anyone in
});

test("native push is registered for the person and a tap on a notification opens its alarm", async ({ page }) => {
  const fixtures = seedFixtures(2, { alarm: true });
  await signInNext(page);
  await openNotificationSettings(page);
  await inbox(page).getByRole("button", { name: "Turn on for this device" }).click();
  await expect(inbox(page).getByText("Notifications are on for this device.").first()).toBeVisible();
  const preferences = await (await page.request.get("/api/push/preferences")).json();
  expect(preferences.devices.map(device => device.device_label).length).toBe(1);
  const token = await page.evaluate(() => window.__stub.pushToken);

  // A tap on the notification: the app passes its data to the interface, which opens that alarm.
  await page.evaluate(alarmId => window.__stub.emit("pushNotificationActionPerformed", { notification: { data: { url: `/?alarm=${alarmId}` } } }), fixtures.alarm.alarm_id);
  await expect(page).toHaveURL(new RegExp(`alarm=${fixtures.alarm.alarm_id}`));
  await expect(inbox(page)).toBeVisible();
  // A link that leaves the NVR is never followed.
  await page.evaluate(() => window.__stub.emit("pushNotificationActionPerformed", { notification: { data: { url: "//evil.example/x" } } }));
  await expect(page).not.toHaveURL(/evil\.example/);

  // Turning it off removes the token.
  await page.reload();
  await openNotificationSettings(page);
  await inbox(page).getByRole("button", { name: "Turn off on this device" }).click();
  await expect(inbox(page).getByText("Notifications are off for this device.").first()).toBeVisible();
  expect((await (await page.request.get("/api/push/preferences")).json()).devices).toEqual([]);
  expect(token).toContain("fcm-registration-token");
});
