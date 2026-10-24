// Alarm-driven Live layouts and video wall mode (#112).
const { test, expect, seedFixtures, raiseAlarm, signInNext, trackLiveSockets } = require("./helpers.cjs");

const name = index => `Camera ${String(index).padStart(3, "0")}`;

async function add(page, ...indexes) {
  for (const index of indexes) await page.getByRole("button", { name: new RegExp(`^${name(index)}\\b`) }).click();
}

async function displayed(page) {
  return page.locator(".grid-cell:not([hidden]) .tile").evaluateAll(tiles =>
    tiles
      .map(tile => ({ name: tile.querySelector(".tile-name").textContent, position: Number(tile.parentElement.dataset.position) }))
      .sort((a, b) => a.position - b.position)
      .map(entry => entry.name));
}

test("an alarm brings its camera and neighbours into Live for people who opted in, and Return restores their view exactly", async ({ page }) => {
  seedFixtures(6);
  await signInNext(page, "camera-operator");
  await add(page, 5, 6);
  const before = await displayed(page);
  expect(before).toEqual([name(5), name(6)]);

  // Not opted in: an alarm leaves Live alone.
  raiseAlarm(1, { neighbours: [2, 3] });
  await page.waitForTimeout(3_000);
  expect(await displayed(page)).toEqual(before);

  const toggle = page.getByRole("button", { name: /^Alarm pop-in/ });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  const sockets = trackLiveSockets(page);
  const first = raiseAlarm(1, { neighbours: [2, 3] });
  const banner = page.getByRole("region", { name: "Alarm layout" });
  await expect(banner).toContainText("Gate entry: Camera 001 · Person");
  await expect.poll(() => displayed(page)).toEqual([name(1), name(2), name(3)]);

  // A second alarm waits its turn instead of taking the screen.
  raiseAlarm(4, { neighbours: [], rule: "Back door" });
  await expect(banner.getByRole("button", { name: "Next alarm (1 waiting)" })).toBeVisible();
  expect(await displayed(page)).toEqual([name(1), name(2), name(3)]);
  await banner.getByRole("button", { name: "Next alarm (1 waiting)" }).click();
  await expect(banner).toContainText("Back door: Camera 004");
  await expect.poll(() => displayed(page)).toEqual([name(4)]);
  // Tiles that left released their streams.
  await expect.poll(() => sockets.open(first.camera_id)).toBe(0);
  await expect.poll(() => sockets.open(first.neighbour_ids[0])).toBe(0);

  await banner.getByRole("button", { name: "Return to my view" }).click();
  await expect(banner).toHaveCount(0);
  await expect.poll(() => displayed(page)).toEqual(before);

  // The alarm layout was never saved as the person's workspace.
  await page.reload();
  await expect.poll(() => displayed(page)).toEqual(before);
  await expect(page.getByRole("button", { name: /^Alarm pop-in/ })).toHaveAttribute("aria-pressed", "true");
});

test("administrators choose a camera's neighbours and which rules pop into Live", async ({ page }) => {
  const fixtures = seedFixtures(4, { alarm: true });
  await signInNext(page);
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: name(1), exact: true }).click();
  await page.getByRole("tab", { name: "Neighbours" }).click();
  const form = page.getByRole("form", { name: "Neighbouring cameras" });
  await form.getByLabel("Add a neighbour").selectOption({ label: name(3) });
  await form.getByLabel("Add a neighbour").selectOption({ label: name(2) });
  await form.getByRole("button", { name: `Move ${name(2)} earlier` }).click();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved 2 neighbouring cameras.");
  const saved = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[0]}/neighbours`)).json();
  expect(saved.camera_ids).toEqual([fixtures.camera_ids[1], fixtures.camera_ids[2]]);

  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await page.getByRole("region", { name: "Rules" }).getByRole("button", { name: /Gate entry/ }).click();
  const rule = page.getByRole("form", { name: "Rule settings" });
  await rule.getByRole("checkbox", { name: "Pop alarms into Live" }).check();
  await rule.getByRole("button", { name: "Save" }).click();
  await expect(rule.getByRole("status")).toContainText("Saved.");
  const rules = (await (await page.request.get("/api/rules")).json()).rules;
  expect(rules.find(item => item.name === "Gate entry").pop_into_live).toBe(true);
});

test("video wall mode signs in with a read and media token, rotates shared views and pops alarms in", async ({ page, browser }) => {
  const fixtures = seedFixtures(4);
  await signInNext(page);
  for (const [view, cameras] of [["Lobby", [0, 1]], ["Yard", [2]]]) {
    const ids = cameras.map(index => fixtures.camera_ids[index]);
    const saved = await page.request.put(`/api/live/shared-layouts/${view}`, { data: { camera_ids: ids, grid_size: 4, tile_camera_ids: [...ids, ...Array(4 - ids.length).fill(null)], layout_mode: "auto" } });
    expect(saved.ok(), await saved.text()).toBe(true);
  }
  const token = async scopes => (await (await page.request.post("/api/admin/api-tokens", { data: { name: `Wall ${scopes.join(" ")}`, scopes } })).json()).secret;
  const wallToken = await token(["read", "media"]);
  const tooBroad = await token(["read", "media", "export"]);

  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const wall = await context.newPage();
  const sockets = trackLiveSockets(wall);
  await wall.goto("/wall?view=Lobby&view=Yard&dwell=5");
  const signIn = wall.getByRole("form", { name: "Video wall sign-in" });
  await signIn.getByLabel("API token").fill(tooBroad);
  await signIn.getByRole("button", { name: "Sign in" }).click();
  await expect(signIn.getByRole("alert")).toContainText("exactly the read and media scopes");
  await signIn.getByLabel("API token").fill(wallToken);
  await signIn.getByRole("button", { name: "Sign in" }).click();

  const screen = wall.getByRole("main", { name: "Video wall" });
  const tiles = () => screen.locator(".wall-tile .tile-name").allTextContents();
  await expect.poll(tiles).toEqual([name(1), name(2)]);
  await expect(screen).toContainText("Lobby · 1 of 2");
  // No menus or navigation on a wall.
  await expect(wall.getByRole("navigation")).toHaveCount(0);
  await expect(wall.getByRole("button")).toHaveCount(0);

  // The rotation moves on, and tiles that left release their streams.
  await expect.poll(tiles, { timeout: 10_000 }).toEqual([name(3)]);
  await expect(screen).toContainText("Yard · 2 of 2");
  await expect.poll(() => sockets.open(fixtures.camera_ids[0])).toBe(0);

  // An alarm pops in over the rotation, with its neighbours.
  raiseAlarm(4, { neighbours: [1] });
  await expect(screen.getByRole("alert")).toContainText("Gate entry: Camera 004 · Person");
  await expect.poll(tiles).toEqual([name(4), name(1)]);

  // The wall's session can watch and nothing more; the main interface does not open with it.
  expect((await wall.request.get("/api/rules")).status()).toBe(403);
  expect((await wall.request.get("/api/auth/me")).status()).toBe(401);
  await context.close();
});
