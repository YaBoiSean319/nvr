// Real-browser journeys for the face watch (#253): the Settings page (switches, threshold, list) and choosing a face in Playback. The face models are not installed in this environment,
// so finding faces in a recording is answered by the test; everything else is the real API, including what is stored and audited.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const PICTURE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const section = (page, name) => page.getByRole("region", { name });

test("an administrator switches a camera on, sets the threshold, and sees why nothing can be added without the models", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/faces");
  await expect(section(page, "Looking for faces")).toContainText("It is an aid, not proof");
  await expect(section(page, "Looking for faces")).toContainText("The face models are not installed in this image");
  await expect(section(page, "Watch list")).toContainText("Nobody is on the list");

  const camera = page.getByRole("checkbox", { name: "Look for watched faces on Camera 001" });
  await expect(camera).not.toBeChecked();
  await camera.click();
  await expect(camera).toBeChecked();
  await page.reload();
  await expect(page.getByRole("checkbox", { name: "Look for watched faces on Camera 001" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Look for watched faces on Camera 002" })).not.toBeChecked();

  await page.getByRole("slider", { name: "Match threshold" }).fill("0.6");
  await page.getByRole("button", { name: "Save threshold" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("slider", { name: "Match threshold" })).toHaveValue("0.6");
});

test("a face chosen in Playback is added with a name and reason, and the person can be switched off and deleted", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const sent = [];
  await page.route("**/api/faces/find", route => route.fulfill({ json: {
    camera_id: "x", at: new Date().toISOString(), picture: PICTURE,
    faces: [{ box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, score: 0.95, pixels: [80, 90], usable: true, reason: null },
            { box: { x: 0.6, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [70, 80], usable: false, reason: "This face is inside a privacy mask" }] } }));
  await page.route("**/api/faces/watch", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    sent.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: { id: "e1", name: "Visitor A", reason: "Trespassed", expires_at: null, expired: false, enabled: true, embeddings: 1, has_picture: false, created_by: "admin", created_at: new Date().toISOString(), updated_at: new Date().toISOString() } });
  });

  await page.goto("/playback");
  await page.getByRole("button", { name: /^Camera 001/ }).click();
  await page.getByRole("button", { name: "Find a face…" }).click();
  const panel = page.getByRole("complementary", { name: "Face watch" });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await expect(panel).toContainText("2 faces found");
  await expect(panel.getByRole("button", { name: /^Face 2, not usable: This face is inside a privacy mask/ })).toBeDisabled();

  await panel.getByRole("button", { name: "Face 1", exact: true }).click();
  const form = panel.getByRole("form", { name: "Add this face" });
  const add = form.getByRole("button", { name: "Add to the face watch list" });
  await expect(add).toBeDisabled(); // A name and a reason are required
  await form.getByLabel("Name or label").fill("Visitor A");
  await expect(add).toBeDisabled();
  await form.getByLabel("Why they are on the list").fill("Trespassed");
  await add.click();
  await expect(panel.getByRole("status").filter({ hasText: "Visitor A is on the face watch list" })).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({ name: "Visitor A", reason: "Trespassed", box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 } });
  expect(sent[0].entry_id).toBeUndefined();
});

test("a person on the list can be switched off and deleted, and the page says how much is removed", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const now = new Date().toISOString();
  let entries = [{ id: "e1", name: "Visitor A", reason: "Trespassed 1 November", expires_at: null, expired: false, enabled: true, embeddings: 2, has_picture: false, created_by: "admin", created_at: now, updated_at: now }];
  const patches = [];
  await page.route("**/api/faces/watch", route => (route.request().method() === "GET" ? route.fulfill({ json: { entries, match_threshold: 0.45 } }) : route.fallback()));
  await page.route("**/api/faces/watch/e1", async route => {
    if (route.request().method() === "PATCH") {
      patches.push(route.request().postDataJSON());
      entries = [{ ...entries[0], ...route.request().postDataJSON() }];
      return route.fulfill({ json: entries[0] });
    }
    entries = [];
    return route.fulfill({ status: 204 });
  });
  await page.goto("/settings/faces");
  const entry = page.getByRole("listitem", { name: "Watched face Visitor A" });
  await expect(entry).toContainText("Trespassed 1 November");
  await expect(entry).toContainText("2 pictures");
  await entry.getByRole("checkbox", { name: "Look for this person" }).click(); // It changes when the server has answered
  await expect(entry).toContainText("switched off");
  expect(patches).toEqual([{ enabled: false }]);
  await entry.getByRole("button", { name: "Delete…" }).click();
  await entry.getByRole("button", { name: /^Yes, delete Visitor A/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "Deleted, with their pictures" })).toBeVisible();
  await expect(section(page, "Watch list")).toContainText("Nobody is on the list");
});

test("the rule editor offers the face condition, with the people on the list to choose from", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const now = new Date().toISOString();
  const entry = (id, name, extra = {}) => ({ id, name, reason: "Trespassed", expires_at: null, expired: false, enabled: true, embeddings: 1, has_picture: false, created_by: "admin", created_at: now, updated_at: now, ...extra });
  await page.route("**/api/faces/watch", route => route.fulfill({ json: { entries: [entry("e1", "Visitor A"), entry("e2", "Visitor B", { enabled: false })], match_threshold: 0.45 } }));
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await page.getByRole("button", { name: "New rule" }).click();
  const editor = page.getByRole("region", { name: /^Rule: / });
  await editor.getByLabel("Name").fill("Trespassers");
  await editor.getByRole("checkbox", { name: "Camera 001" }).check();
  await editor.getByLabel("When an object").selectOption("face");
  await expect(editor).toContainText("Fires when a face on the face watch list is recognized");
  await expect(editor.getByRole("checkbox", { name: "Visitor B (off)" })).toBeVisible();
  await editor.getByRole("checkbox", { name: "Visitor A" }).check();
  const saved = page.waitForResponse(response => response.url().endsWith("/api/rules") && response.request().method() === "POST");
  await editor.getByRole("button", { name: "Save" }).click();
  const body = (await saved).request().postDataJSON();
  expect(body.condition).toEqual({ type: "face", entry_ids: ["e1"] });
  await expect(editor.getByRole("status")).toContainText("Saved.");
});

test("matches are fetched when asked for, and a save elsewhere on the page does not fetch them or write another audit row", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  let fetched = 0;
  await page.route("**/api/faces/sightings", route => { fetched += 1; return route.continue(); });
  await page.goto("/settings/faces");
  await expect(section(page, "Matches found")).toBeVisible();
  expect(fetched).toBe(0); // Nothing is fetched, or audited, just by opening the page

  await section(page, "Matches found").getByRole("button", { name: "Show recent matches" }).click();
  await expect(section(page, "Matches found").getByText("No matches yet.")).toBeVisible();
  expect(fetched).toBe(1);

  await page.getByRole("checkbox", { name: "Look for watched faces on Camera 001" }).click();
  await expect(page.getByRole("checkbox", { name: "Look for watched faces on Camera 001" })).toBeChecked();
  await page.getByRole("slider", { name: "Match threshold" }).fill("0.5");
  await page.getByRole("button", { name: "Save threshold" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));  // What the query library listens to for a return to the window
  expect(fetched).toBe(1); // Switching a camera, saving and coming back to the window left the matches alone

  await section(page, "Matches found").getByRole("button", { name: "Refresh matches" }).click();
  await expect.poll(() => fetched).toBe(2); // An explicit refresh is a request to look
});

test("the alert rule is offered once for the switched-on cameras no face rule covers, and cannot be made twice", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  const now = new Date().toISOString();
  await page.route("**/api/faces/watch", route => route.fulfill({ json: { entries: [{ id: "e1", name: "Visitor A", reason: "Trespassed", expires_at: null, expired: false, enabled: true, embeddings: 1, has_picture: false, created_by: "admin", created_at: now, updated_at: now }], match_threshold: 0.45 } }));
  let made = 0;
  await page.route("**/api/rules", route => { if (route.request().method() === "POST") made += 1; return route.continue(); });
  await page.goto("/settings/faces");
  const create = page.getByRole("button", { name: "Create an alert rule for these cameras" });
  await expect(create).toHaveCount(0); // No camera is looking yet

  await page.getByRole("checkbox", { name: "Look for watched faces on Camera 001" }).click();
  await expect(create).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "One camera is looking for watched faces" })).toBeVisible();
  await create.dblclick(); // Two clicks in a row make one rule
  await expect(create).toHaveCount(0);
  expect(made).toBe(1);

  await page.getByRole("checkbox", { name: "Look for watched faces on Camera 002" }).click(); // A camera switched on afterwards is not in that rule
  await expect(page.getByRole("status").filter({ hasText: "One camera is looking for watched faces" })).toBeVisible();
  await create.click();
  await expect(create).toHaveCount(0);
  expect(made).toBe(2);
});

/** The Playback drawer with the picture at the playhead answered by the test: `faces` as the server would list them, and the adds it is sent. */
async function openFaceDrawer(page, { faces, picture = PICTURE, addStatus = 201 }) {
  const sent = [];
  const now = new Date().toISOString();
  await page.route("**/api/faces/find", route => route.fulfill({ json: { camera_id: route.request().postDataJSON().camera_id, at: now, picture, faces } }));
  await page.route("**/api/faces/watch", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    sent.push(route.request().postDataJSON());
    return addStatus === 201
      ? route.fulfill({ status: 201, json: { id: "e1", name: "Visitor A", reason: "Trespassed", expires_at: null, expired: false, enabled: true, embeddings: 1, has_picture: false, created_by: "admin", created_at: now, updated_at: now } })
      : route.fulfill({ status: addStatus, json: { detail: "That did not work" } });
  });
  await page.goto("/playback");
  await page.getByRole("button", { name: /^Camera 001/ }).click();
  await page.getByRole("button", { name: "Find a face…" }).click();
  const panel = page.getByRole("complementary", { name: "Face watch" });
  await expect(panel).toBeVisible();
  return { panel, sent };
}

test("with one face box inside another, the smaller face is the one chosen, whatever order they were found in, and it can be reached by keyboard", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const small = { x: 0.4, y: 0.4, width: 0.1, height: 0.1 };
  const large = { x: 0.2, y: 0.2, width: 0.5, height: 0.5 };
  // The larger box is found last, so drawn in found order it would lie over the smaller one.
  const { panel, sent } = await openFaceDrawer(page, { faces: [{ box: small, score: 0.9, pixels: [60, 60], usable: true, reason: null }, { box: large, score: 0.8, pixels: [200, 200], usable: true, reason: null }] });
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await expect(panel).toContainText("2 faces found");

  const frame = await panel.locator(".face-picture").boundingBox();
  await page.mouse.click(frame.x + frame.width * 0.45, frame.y + frame.height * 0.45); // Inside both boxes
  await expect(panel.getByRole("button", { name: "Face 1", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(panel.getByRole("status").filter({ hasText: "Face 1 is chosen" })).toBeVisible(); // Marked before the form is filled in
  const form = panel.getByRole("form", { name: "Add this face" });
  await form.getByLabel("Name or label").fill("Visitor A");
  await form.getByLabel("Why they are on the list").fill("Trespassed");
  await form.getByRole("button", { name: "Add to the face watch list" }).click();
  await expect(panel).toContainText("Visitor A is on the face watch list");
  expect(sent[0].box).toEqual(small);

  // Every usable face is a button that can be focused and pressed from the keyboard.
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await expect(panel).toContainText("2 faces found");
  await panel.getByRole("button", { name: "Face 2", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("button", { name: "Face 2", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("when faces were found but the picture could not be made, nothing can be chosen, and the page says so differently from no face being found", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const { panel } = await openFaceDrawer(page, { picture: null, faces: [{ box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [80, 90], usable: true, reason: null }] });
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await expect(panel.getByRole("alert")).toContainText("The picture could not be shown");
  await expect(panel).not.toContainText("faces found. Choose one");
  await expect(panel).not.toContainText("1 face found");
  await expect(panel.getByRole("button", { name: /^Face 1/ })).toHaveCount(0);
  await expect(panel.getByRole("form", { name: "Add this face" })).toHaveCount(0);
  await expect(panel).not.toContainText("No face was found"); // That is for a picture with no face in it

  await panel.getByRole("button", { name: "Look at the playhead" }).click(); // Trying again asks again
  await expect(panel.getByRole("alert")).toContainText("The picture could not be shown");
});

test("each attempt to add a face starts clean: an old error does not carry over, and a person deleted meanwhile is no longer selected", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const now = new Date().toISOString();
  let entries = [{ id: "e1", name: "Visitor A", reason: "Trespassed", expires_at: null, expired: false, enabled: true, embeddings: 1, has_picture: false, created_by: "admin", created_at: now, updated_at: now }];
  await page.route("**/api/faces/watch", route => (route.request().method() === "GET" ? route.fulfill({ json: { entries, match_threshold: 0.45 } }) : route.fallback()));
  const faces = [{ box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [80, 90], usable: true, reason: null }, { box: { x: 0.6, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [70, 80], usable: true, reason: null }];
  const { panel } = await openFaceDrawer(page, { faces, addStatus: 422 });
  const look = panel.getByRole("button", { name: "Look at the playhead" });
  await look.click();
  await panel.getByRole("button", { name: "Face 1", exact: true }).click();
  const form = panel.getByRole("form", { name: "Add this face" });
  await form.getByLabel("Name or label").fill("Visitor B");
  await form.getByLabel("Why they are on the list").fill("Trespassed");
  await form.getByRole("button", { name: "Add to the face watch list" }).click();
  await expect(form.getByRole("alert")).toContainText("That did not work");

  await look.click(); // Looking again clears it
  await panel.getByRole("button", { name: "Face 2", exact: true }).click();
  await expect(panel.getByRole("form", { name: "Add this face" }).getByRole("alert")).toHaveCount(0);

  await form.getByLabel("Name or label").fill("Visitor B");
  await form.getByLabel("Why they are on the list").fill("Trespassed");
  await form.getByRole("button", { name: "Add to the face watch list" }).click();
  await expect(form.getByRole("alert")).toBeVisible();
  await panel.getByRole("button", { name: "Face 1", exact: true }).click(); // Picking a different face clears it too
  await expect(form.getByRole("alert")).toHaveCount(0);

  await form.getByRole("radio", { name: "Another picture of someone on the list" }).check();
  await form.getByLabel("Who").selectOption("e1");
  await expect(form.getByRole("button", { name: "Add to the face watch list" })).toBeEnabled();
  entries = []; // Deleted in Settings in the meantime
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));  // The list is read again when the window is returned to
  await expect(form.getByLabel("Who")).toHaveValue("");
  await expect(form.getByRole("button", { name: "Add to the face watch list" })).toBeDisabled();
});

test("the expiry date field offers nothing before today", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  const { panel } = await openFaceDrawer(page, { faces: [{ box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [80, 90], usable: true, reason: null }] });
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await panel.getByRole("button", { name: "Face 1", exact: true }).click();
  const today = await page.evaluate(() => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`; });
  await expect(panel.getByLabel("Remove from the list after (optional)")).toHaveAttribute("min", today);
});

test("the picture says which camera and moment it is from, and changing the camera clears it, its boxes and the chosen face", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  const { panel } = await openFaceDrawer(page, { faces: [{ box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, score: 0.9, pixels: [80, 90], usable: true, reason: null }] });
  await page.getByRole("button", { name: /^Camera 002/ }).click(); // Both cameras are shown in Playback
  await panel.getByLabel("Camera").selectOption({ label: "Camera 001" });
  await panel.getByRole("button", { name: "Look at the playhead" }).click();
  await expect(panel.getByRole("img", { name: "The picture at the playhead" })).toBeVisible();
  await expect(panel.getByText(/^Camera 001 · .*\d/)).toBeVisible(); // The camera, and the moment as the site writes it
  await panel.getByRole("button", { name: "Face 1", exact: true }).click();
  await expect(panel.getByRole("form", { name: "Add this face" })).toBeVisible();

  await panel.getByLabel("Camera").selectOption({ label: "Camera 002" });
  await expect(panel.getByRole("img", { name: "The picture at the playhead" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: /^Face 1/ })).toHaveCount(0);
  await expect(panel.getByRole("form", { name: "Add this face" })).toHaveCount(0);
  await expect(panel.getByText(/^Camera 001 · /)).toHaveCount(0);
});
