// Real-browser journeys for passkeys, authenticator apps and recovery codes (#118).
const crypto = require("node:crypto");
const { test, expect, FIXTURE_PASSWORD, seedFixtures, signInNext } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

function base32Decode(text) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const character of text.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(character).toString(2).padStart(5, "0");
  const bytes = [];
  for (let index = 0; index + 8 <= bits.length; index += 8) bytes.push(parseInt(bits.slice(index, index + 8), 2));
  return Buffer.from(bytes);
}

const step = (offset = 0) => Math.floor(Date.now() / 30_000) + offset;

/** The six-digit code an authenticator app shows for a time step (RFC 6238). */
function totp(secret, counter) {
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = crypto.createHmac("sha1", secret).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

async function openSecurity(page, username) {
  await page.getByRole("button", { name: `Account menu for ${username}` }).click();
  await page.getByRole("menuitem", { name: "Security…" }).click();
  const dialog = page.getByRole("dialog", { name: "Account security" });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function signOut(page, username) {
  await page.getByRole("button", { name: `Account menu for ${username}` }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

async function passwordStep(page, username) {
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(FIXTURE_PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Verify it's you" })).toBeVisible();
}

/** Set up an authenticator app in the open Security dialog (or enrolment screen); returns its secret and the recovery codes shown. */
async function setUpAuthenticator(page, scope) {
  await scope.getByRole("button", { name: "Set up an authenticator app" }).click();
  const secret = base32Decode(await scope.locator('[aria-label="Setup key"]').innerText());
  await scope.getByLabel("Code from the app").fill(totp(secret, step()));
  await scope.getByRole("button", { name: "Confirm" }).click();
  const codes = page.getByRole("dialog", { name: "Save your recovery codes" });
  await expect(codes).toBeVisible();
  const recovery = await codes.getByRole("listitem").allInnerTexts();
  await codes.getByRole("button", { name: "I have saved them" }).click();
  return { secret, recovery };
}

test("an authenticator app: set up from the account menu, used to sign in, and each recovery code works once", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const dialog = await openSecurity(page, "camera-admin");
  const { secret, recovery } = await setUpAuthenticator(page, dialog);
  expect(recovery).toHaveLength(10);
  await expect(dialog.getByText("Signing in asks for a second factor.")).toBeVisible();
  await expect(dialog.getByText("10 of 10 recovery codes left")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await signOut(page, "camera-admin");

  // The password alone is not enough, and a wrong code is refused.
  await passwordStep(page, "camera-admin");
  await page.getByLabel("Authentication code").fill("000000");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert")).toContainText("That code is not right");
  await page.getByLabel("Authentication code").fill(totp(secret, step(1)));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(nav(page)).toBeVisible();
  await signOut(page, "camera-admin");

  // A recovery code signs in once.
  await passwordStep(page, "camera-admin");
  await page.getByRole("button", { name: "Use a recovery code" }).click();
  await page.getByLabel("Recovery code").fill(recovery[0]);
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(nav(page)).toBeVisible();
  await signOut(page, "camera-admin");

  await passwordStep(page, "camera-admin");
  await page.getByRole("button", { name: "Use a recovery code" }).click();
  await page.getByLabel("Recovery code").fill(recovery[0]);
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert")).toContainText("That code is not right");
  await page.getByLabel("Recovery code").fill(recovery[1]);
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(nav(page)).toBeVisible();

  // Removing the app asks for the password again.
  const security = await openSecurity(page, "camera-admin");
  await expect(security.getByText("8 of 10 recovery codes left")).toBeVisible();
  await security.getByRole("button", { name: "Remove", exact: true }).click();
  const confirm = page.getByRole("dialog", { name: "Remove the authenticator app" });
  await confirm.getByLabel("Your password").fill("not the password 123");
  await confirm.getByRole("button", { name: "Confirm" }).click();
  await expect(confirm.getByRole("alert")).toContainText("That password is not right");
  await confirm.getByLabel("Your password").fill(FIXTURE_PASSWORD);
  await confirm.getByRole("button", { name: "Confirm" }).click();
  await expect(security.getByRole("button", { name: "Set up an authenticator app" })).toBeVisible();
});

test("a passkey: added with a virtual authenticator, used as the second step, and then alone", async ({ browser, baseURL }) => {
  seedFixtures(1);
  // WebAuthn needs a host name, not an IP address: the same server, reached as localhost.
  const context = await browser.newContext({ baseURL: baseURL.replace("127.0.0.1", "localhost") });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
  });

  await signInNext(page);
  const dialog = await openSecurity(page, "camera-admin");
  await dialog.getByLabel("Name").fill("Virtual key");
  await dialog.getByRole("button", { name: "Add a passkey" }).click();
  const codes = page.getByRole("dialog", { name: "Save your recovery codes" });
  await expect(codes.getByRole("listitem")).toHaveCount(10);
  await codes.getByRole("button", { name: "I have saved them" }).click();
  await expect(dialog.getByRole("list", { name: "Your passkeys" })).toContainText("Virtual key");
  await dialog.getByRole("button", { name: "Close" }).click();
  await signOut(page, "camera-admin");

  // Password, then the passkey.
  await passwordStep(page, "camera-admin");
  await page.getByRole("button", { name: "Use passkey" }).click();
  await expect(nav(page)).toBeVisible();

  // Turning on passkey-only sign-in asks for the password, and then a passkey is enough.
  const security = await openSecurity(page, "camera-admin");
  await security.getByLabel("Let me sign in with only a passkey").click(); // Flips once the password is confirmed.
  const confirm = page.getByRole("dialog", { name: "Sign in with only a passkey" });
  await confirm.getByLabel("Your password").fill(FIXTURE_PASSWORD);
  await confirm.getByRole("button", { name: "Confirm" }).click();
  await expect(security.getByLabel("Let me sign in with only a passkey")).toBeChecked();
  await security.getByRole("button", { name: "Close" }).click();
  await signOut(page, "camera-admin");

  await page.getByRole("button", { name: "Use a passkey" }).click();
  await expect(nav(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Account menu for camera-admin" })).toBeVisible();
  await context.close();
});

test("passkeys explain themselves when the NVR is opened by IP address", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const dialog = await openSecurity(page, "camera-admin");
  await dialog.getByRole("button", { name: "Add a passkey" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Passkeys do not work at this web address");
});

test("when required, someone without a second factor must set one up first; an administrator can reset it", async ({ page, browser }) => {
  seedFixtures(1);
  await signInNext(page);
  // The administrator has a factor of their own before asking others for one.
  const own = await openSecurity(page, "camera-admin");
  await setUpAuthenticator(page, own);
  await own.getByRole("button", { name: "Close" }).click();

  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Users & access" }).click();
  const policy = page.getByRole("region", { name: "Sign-in security" });
  await policy.getByLabel("Second factor for local accounts").selectOption("administrators");
  await expect(policy.getByRole("status")).toContainText("required for administrators");

  // Administrators only: an operator signs straight in.
  const operatorContext = await browser.newContext({ baseURL: page.url().split("/").slice(0, 3).join("/") });
  const operator = await operatorContext.newPage();
  await operator.goto("/");
  await operator.getByLabel("Username").fill("camera-operator");
  await operator.getByLabel("Password").fill(FIXTURE_PASSWORD);
  await operator.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(nav(operator)).toBeVisible();

  // Required for all: the operator, already signed in, reaches nothing but enrolment on their next load.
  await policy.getByLabel("Second factor for local accounts").selectOption("all");
  await expect(policy.getByRole("status")).toContainText("required for all local accounts");
  await operator.reload();
  await expect(operator.getByRole("heading", { name: "Set up a second factor" })).toBeVisible();
  await expect(nav(operator)).toHaveCount(0);
  expect((await operator.request.get("/api/cameras")).status()).toBe(403);
  await setUpAuthenticator(operator, operator.locator(".auth-panel"));
  await expect(nav(operator)).toBeVisible();
  expect((await operator.request.get("/api/cameras")).status()).toBe(200);

  // The administrator sees the new factor, and can take it away.
  await page.reload();
  const accounts = page.getByRole("region", { name: "Sign-in security" });
  const row = accounts.getByRole("row", { name: /camera-operator/ });
  await expect(row).toContainText("authenticator app");
  await row.getByRole("button", { name: "Reset second factors for camera-operator" }).click();
  await page.getByRole("dialog", { name: /Reset second factors for camera-operator/ }).getByRole("button", { name: "Reset second factors" }).click();
  await expect(accounts.getByRole("status")).toContainText("Second factors removed for camera-operator");
  await expect(row).toContainText("None");
  expect((await operator.request.get("/api/auth/me")).status()).toBe(401); // Their session ended.
  await operatorContext.close();
});
