// Real-browser journey for email alerts through Microsoft sign-in (#231). The sign-in and Graph calls are covered against a stand-in Microsoft by the unit tests;
// here the settings API is scripted so the page's flow is checked: tenant in, link and code shown, page continues by itself, test email.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("an administrator connects Microsoft 365 with a code and a link, and sends a test email", async ({ page }) => {
  seedFixtures(1);
  let state = { state: "none", limits: { per_rule_per_hour: 20, per_hour: 100 } };
  let polls = 0;
  const sent = [];
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/admin/email", route => (route.request().method() === "GET" ? json(route, state) : route.fulfill({ status: 204 })));
  await page.route("**/api/admin/email/connect", route => {
    const body = route.request().postDataJSON();
    sent.push(body);
    if (!/^[0-9a-f-]{36}$/i.test(body.client_id)) return json(route, { detail: "Enter the application (client) ID from the app registration: a GUID" }, 422);
    const sign_in = { user_code: "ABCD-EFGH", verification_uri: "https://microsoft.com/devicelogin", expires_at: new Date(Date.now() + 600_000).toISOString(), interval: 2 };
    state = { ...state, state: "pending", tenant_id: body.tenant_id, client_id: body.client_id, sender: null, sign_in };
    return json(route, { state: "pending", sign_in });
  });
  await page.route("**/api/admin/email/poll", route => {
    polls += 1;
    if (polls >= 2) state = { state: "connected", limits: state.limits, tenant_id: state.tenant_id, client_id: state.client_id, account: "alerts@contoso.com", sender: null };
    return json(route, state);
  });
  await page.route("**/api/admin/email/incidents", route => {
    const body = route.request().postDataJSON();
    sent.push({ incidents: body });
    state = { ...state, incidents: { enabled: body.enabled, recipients: body.recipients }, held_back: { count: 3, since: new Date(Date.now() - 3600_000).toISOString() } };
    return json(route, state);
  });
  await page.route("**/api/admin/email/test", route => {
    sent.push(route.request().postDataJSON());
    return json(route, { sent: true, to: "desk@contoso.com", at: new Date().toISOString(), sent_as: "alerts@contoso.com", account: "alerts@contoso.com", request_id: "req-123", saved_to_sent_items: true, ...state });
  });

  await signInNext(page);
  await page.goto("/settings/email");
  const connection = page.getByRole("region", { name: "Microsoft 365 connection" });
  await connection.getByLabel("Tenant ID or domain").fill("contoso.onmicrosoft.com");
  await connection.getByLabel("Application (client) ID").fill("not-a-guid");
  await connection.getByRole("button", { name: "Connect with Microsoft" }).click();
  await expect(page.getByRole("alert")).toContainText("application (client) ID");

  await connection.getByLabel("Application (client) ID").fill("11111111-2222-3333-4444-555555555555");
  await connection.getByRole("button", { name: "Connect with Microsoft" }).click();
  const signIn = page.getByRole("status", { name: "Sign in with Microsoft" });
  await expect(signIn).toContainText("ABCD-EFGH");
  await expect(signIn.getByRole("link", { name: "https://microsoft.com/devicelogin" })).toHaveAttribute("href", "https://microsoft.com/devicelogin");

  await expect(connection.getByRole("status").filter({ hasText: "Connected as" })).toContainText("alerts@contoso.com", { timeout: 15_000 });
  await expect(page.locator("body")).not.toContainText("ABCD-EFGH");

  const test_ = page.getByRole("region", { name: "Send a test email" });
  await test_.getByLabel("Send a test email to").fill("desk@contoso.com");
  await test_.getByRole("button", { name: "Send test email" }).click();
  const accepted = page.getByRole("status").filter({ hasText: "Microsoft accepted a test email from alerts@contoso.com to desk@contoso.com" });
  await expect(accepted).toBeVisible();
  await expect(accepted).toContainText("A copy is in that account's Sent Items.");
  await expect(accepted).toContainText("look in its Junk folder");
  await expect(accepted).toContainText("message trace");
  await expect(accepted).toContainText("Microsoft's request id: req-123.");
  expect(sent.at(-1)).toEqual({ to: "desk@contoso.com" });

  const problems = page.getByRole("region", { name: "Emails about NVR problems" });
  await problems.getByLabel("Email these problems").check();
  await problems.getByLabel("Send to").fill("it@contoso.com, desk@contoso.com");
  await problems.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(sent.at(-1)).toEqual({ incidents: { enabled: true, recipients: ["it@contoso.com", "desk@contoso.com"] } });
  await expect(problems).toContainText("3 alert emails were held back by the hourly limit");
  expect(sent[0]).toMatchObject({ tenant_id: "contoso.onmicrosoft.com" });
});
