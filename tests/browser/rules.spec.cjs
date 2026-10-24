// Real-browser journeys for Rules (#108), operated by keyboard. The detections fixture stores the
// detection scene on Camera 001 an hour ago: a red person walks left to right across the frame,
// a green person right to left, and a blue vehicle right to left along the bottom.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const workspace = page => page.getByRole("region", { name: "Rules" });
const editor = page => page.getByRole("region", { name: /^Rule: / });
const settings = page => editor(page).getByRole("form", { name: "Rule settings" });

async function press(locator, ...keys) {
  await locator.focus();
  for (const key of keys) await locator.page().keyboard.press(key);
}

async function rules(page) {
  return (await (await page.request.get("/api/rules")).json()).rules;
}

test("an administrator creates, tests, disables and deletes a line rule by keyboard", async ({ page }) => {
  const fixtures = seedFixtures(2, { detections: true });
  await signInNext(page);
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await expect(workspace(page)).toContainText("No rules yet.");

  await press(workspace(page).getByRole("button", { name: "New rule" }), "Enter");
  await expect(editor(page).getByRole("heading", { name: "New rule" })).toBeVisible();
  await editor(page).getByLabel("Name").focus();
  await page.keyboard.type("Corridor eastbound");
  await press(editor(page).getByRole("checkbox", { name: "Camera 001" }), "Space");
  await press(editor(page).getByLabel("When an object"), "ArrowDown");
  await expect(editor(page).getByLabel("When an object")).toHaveValue("line_crossing");
  await press(editor(page).getByLabel("Direction"), "ArrowDown");
  await expect(editor(page).getByLabel("Direction")).toHaveValue("forward");

  // The default line runs down the middle of the frame; nudge its lower end left.
  const endB = editor(page).getByRole("button", { name: /^Line end B:/ });
  await expect(endB).toHaveAccessibleName("Line end B: 50% from left, 90% from top");
  await press(endB, "Shift+ArrowLeft");
  await expect(endB).toHaveAccessibleName("Line end B: 45% from left, 90% from top");

  // Test mode replays the stored scene: only the red person crosses with the arrow.
  const testing = page.getByRole("region", { name: "Test mode" });
  await press(testing.getByRole("button", { name: "Test against the last 24 hours" }), "Enter");
  await expect(testing.getByRole("status")).toContainText("The rule would have fired 1 time.");
  await expect(testing.getByRole("table")).toContainText("Camera 001");
  expect(await rules(page)).toEqual([]);

  await press(editor(page).getByRole("button", { name: "Save" }), "Enter");
  await expect(settings(page).getByRole("status")).toContainText("Saved.");
  await expect(workspace(page).getByRole("button", { name: /Corridor eastbound/ })).toContainText("Crosses a line · people");
  let saved = await rules(page);
  expect(saved).toHaveLength(1);
  expect(saved[0].camera_ids).toEqual([fixtures.camera_ids[0]]);
  expect(saved[0].condition).toEqual({ type: "line_crossing", line: { x1: 0.5, y1: 0.1, x2: 0.45, y2: 0.9 }, direction: "forward" });

  await press(editor(page).getByRole("checkbox", { name: "Enabled" }), "Space");
  await press(editor(page).getByRole("button", { name: "Save" }), "Enter");
  await expect(settings(page).getByRole("status")).toContainText("The rule is disabled.");
  await expect(workspace(page).getByRole("button", { name: /Corridor eastbound/ })).toContainText("disabled");
  saved = await rules(page);
  expect(saved[0].enabled).toBe(false);

  await press(editor(page).getByRole("button", { name: "Delete rule…" }), "Enter");
  await press(page.getByRole("dialog").getByRole("button", { name: "Delete rule" }), "Enter");
  await expect(workspace(page)).toContainText("No rules yet.");
  expect(await rules(page)).toEqual([]);
});

test("unsaved rule edits are protected and invalid rules cannot be saved", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/cameras/rules");
  await workspace(page).getByRole("button", { name: "New rule" }).click();
  await editor(page).getByLabel("Name").fill("Gate dwell");
  // A rule needs cameras to watch before it can be saved or tested.
  await expect(settings(page).getByRole("alert")).toHaveText("Choose at least one camera or group.");
  await expect(editor(page).getByRole("button", { name: "Save" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Test against the last 24 hours" })).toBeDisabled();

  await editor(page).getByRole("checkbox", { name: "Group: Operations" }).check();
  await editor(page).getByLabel("When an object").selectOption("loitering");
  await expect(editor(page).getByLabel("For at least (seconds)")).toHaveValue("30");
  await editor(page).getByLabel("For at least (seconds)").fill("");
  await expect(settings(page).getByRole("alert")).toHaveText("Enter how many seconds an object must stay.");
  await editor(page).getByLabel("For at least (seconds)").fill("45");
  // A test result goes away as soon as the rule it describes changes.
  const testing = page.getByRole("region", { name: "Test mode" });
  await testing.getByRole("button", { name: "Test against the last 24 hours" }).click();
  await expect(testing.getByRole("status")).toContainText("The rule would not have fired.");
  await editor(page).getByLabel("For at least (seconds)").fill("60");
  await expect(testing.getByRole("status")).toHaveCount(0);
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Groups & Policies" }).click();
  const leave = page.getByRole("dialog");
  await expect(leave).toContainText("the new rule");
  await leave.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/cameras\/groups$/);
  const saved = await rules(page);
  expect(saved.map(rule => [rule.name, rule.condition.type, rule.condition.dwell_seconds, rule.group_ids.length])).toEqual([["Gate dwell", "loitering", 60, 1]]);
});

test("operators do not see Rules and cannot open it", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page, "camera-operator");
  await page.goto("/cameras");
  await expect(page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" })).toHaveCount(0);
  await page.goto("/cameras/rules");
  await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  expect((await page.request.get("/api/rules")).status()).toBe(403);
});
