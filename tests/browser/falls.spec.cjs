// Real-browser journeys for fall detection (#230): the settings page says what it is and is not, cameras are chosen with their sensitivity and stay-down time, and a rule
// "A person fell" is made. The pose model is not installed in this environment, so the page's installed state is shown with the API's answer for it mocked.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const section = page => page.getByRole("region", { name: "Looking for falls" });

test("without the pose model the page says so and a camera cannot be turned on", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/settings/falls");
  await expect(section(page)).toContainText("This is an aid, not a safety system");
  await expect(section(page)).toContainText("will miss some falls");
  await expect(section(page)).toContainText("Apache-2.0");
  await expect(section(page)).toContainText("The pose model is not installed in this image");
  await expect(section(page).getByRole("checkbox", { name: "Look for falls on Camera 001" })).toBeDisabled();
  await expect(page.getByRole("region", { name: "Falls found" })).toContainText("No falls found yet.");
});

test("an administrator turns fall detection on for a camera and chooses its sensitivity and stay-down time", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  let camera = { enabled: false, sensitivity: "high", stay_down_seconds: 3 };
  const puts = [];
  await page.route("**/api/admin/falls", async route => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, model: { ...body.model, installed: true }, cameras: body.cameras.map(item => ({ ...item, ...camera })) } });
  });
  await page.route("**/api/admin/falls/cameras/*", async route => {
    const sent = route.request().postDataJSON();
    puts.push(sent);
    camera = sent;
    await route.fulfill({ json: { id: "x", ...sent } });
  });
  await page.goto("/settings/falls");
  const checkbox = section(page).getByRole("checkbox", { name: "Look for falls on Camera 001" });
  await expect(checkbox).toBeEnabled();
  await expect(checkbox).not.toBeChecked();
  await checkbox.click();
  await expect(page.getByText(/Saved\. The AI worker follows within about half a minute/)).toBeVisible();
  await expect(checkbox).toBeChecked();
  await section(page).getByLabel("Sensitivity for Camera 001").selectOption("low");
  await expect(section(page).getByLabel("Sensitivity for Camera 001")).toHaveValue("low");
  await section(page).getByLabel("Seconds a person must stay down on Camera 001").fill("8");
  await section(page).getByLabel("Seconds a person must stay down on Camera 001").blur();
  await expect.poll(() => puts.length).toBe(3);
  expect(puts).toEqual([
    { enabled: true, sensitivity: "high", stay_down_seconds: 3 },
    { enabled: true, sensitivity: "low", stay_down_seconds: 3 },
    { enabled: true, sensitivity: "low", stay_down_seconds: 8 },
  ]);
});

test("a rule 'A person fell' is made and saved, and the form says what it needs", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/cameras/rules");
  await page.getByRole("region", { name: "Rules" }).getByRole("button", { name: "New rule" }).click();
  const editor = page.getByRole("region", { name: /^Rule: / });
  await editor.getByLabel("Name").fill("Fall in the lounge");
  await editor.getByRole("checkbox", { name: "Camera 001" }).check();
  await editor.getByLabel("When an object").selectOption("fall");
  await expect(editor.getByLabel("When")).toHaveValue("fall");
  await expect(editor).toContainText("It can miss falls and raise alerts that are not falls");
  await expect(editor.getByRole("group", { name: "Objects" })).toHaveCount(0);  // A fall is about people only, so there are no object classes to choose
  await editor.getByRole("button", { name: "Save" }).click();
  await expect.poll(async () => (await (await page.request.get("/api/rules")).json()).rules.map(rule => [rule.name, rule.condition.type])).toEqual([["Fall in the lounge", "fall"]]);
});
