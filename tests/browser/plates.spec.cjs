// Real-browser journeys for licence plates (#142): designating cameras, watch lists, a plate rule that raises alarms for deny-list plates and not allow-list plates,
// and finding a plate in full or in part. The plate reads are seeded as the AI worker leaves them; the plate models themselves are not installed here.
const { test, expect, seedFixtures, seedPlates, signInNext } = require("./helpers.cjs");

const page_ = page => page.getByRole("region", { name: "Licence plates" });
const list = (page, name) => page.getByRole("article", { name: `Watch list ${name}` });
const panel = page => page.getByRole("complementary", { name: "Plate search" });
const results = page => panel(page).getByRole("list", { name: "Plate results" });
const cards = page => results(page).getByRole("button", { name: /^Open/ });

test("an administrator designates a camera, makes watch lists and sees why nothing is read without the models", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/plates");
  await expect(page_(page)).toBeVisible();
  await expect(page.getByText(/The plate models are not installed, so nothing is read yet/)).toBeVisible();

  await page.getByRole("checkbox", { name: "Read plates on Camera 001" }).click();
  await expect(page.getByRole("checkbox", { name: "Read plates on Camera 001" })).toBeChecked();
  await page.reload();
  await expect(page.getByRole("checkbox", { name: "Read plates on Camera 001" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Read plates on Camera 002" })).not.toBeChecked();

  const form = page.getByRole("form", { name: "Add a watch list" });
  await form.getByLabel("List name").fill("Banned");
  await form.getByRole("button", { name: "Add list" }).click();
  await expect(list(page, "Banned")).toContainText("Deny list: raise an alarm for plates on it");
  await form.getByLabel("List name").fill("Residents");
  await form.getByLabel("Kind of list").selectOption("allow");
  await form.getByRole("button", { name: "Add list" }).click();
  await expect(list(page, "Residents")).toContainText("Allow list: raise an alarm for plates not on it");

  await list(page, "Banned").getByLabel("Plate for Banned").fill("kt-55*");
  await list(page, "Banned").getByRole("button", { name: "Add plate to Banned" }).click();
  await expect(list(page, "Banned").getByRole("list", { name: "Plates on Banned" })).toContainText("KT55*");
  await list(page, "Banned").getByLabel("Plate for Banned").fill("?*");
  await list(page, "Banned").getByRole("button", { name: "Add plate to Banned" }).click();
  await expect(page.getByRole("alert")).toContainText("at least two letters or digits");
  await list(page, "Residents").getByLabel("Plate for Residents").fill("AB12CDE");
  await list(page, "Residents").getByRole("button", { name: "Add plate to Residents" }).click();
  await expect(list(page, "Residents").getByRole("list", { name: "Plates on Residents" })).toContainText("AB12CDE");
});

test("a rule on a deny list raises an alarm for a plate on it, and one on an allow list for a plate that is not", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  const made = async (path, body) => (await page.request.post(path, { data: body })).json();
  const banned = await made("/api/admin/plates/watch-lists", { name: "Banned", kind: "deny", lookalikes: false });
  await made(`/api/admin/plates/watch-lists/${banned.id}/entries`, { pattern: "KT55QRS" });
  const residents = await made("/api/admin/plates/watch-lists", { name: "Residents", kind: "allow", lookalikes: false });
  await made(`/api/admin/plates/watch-lists/${residents.id}/entries`, { pattern: "AB12CDE" });

  // The rule editor offers the plate condition and the lists.
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await page.getByRole("button", { name: "New rule" }).click();
  const editor = page.getByRole("region", { name: /^Rule: / });
  await editor.getByLabel("Name").fill("Gate plates");
  await editor.getByRole("checkbox", { name: "Camera 001" }).check();
  await editor.getByLabel("When an object").selectOption("plate");
  await editor.getByLabel("Watch list for the rule").selectOption({ label: "Banned (deny)" });
  await editor.getByRole("button", { name: "Save" }).click();
  await expect(editor.getByRole("status")).toContainText("Saved.");
  const second = await made("/api/rules", { name: "Strangers", camera_ids: [(await (await page.request.get("/api/cameras")).json()).cameras[0].id], condition: { type: "plate", watch_list_id: residents.id } });
  expect(second.id).toBeTruthy();

  seedPlates("read", "AB12CDE", 1);  // A resident: the allow-list rule stays quiet, and the plate is not on the deny list
  seedPlates("read", "KT55QRS", 1);  // On the deny list, and not a resident: both rules raise it
  seedPlates("read", "XX00XXX", 1);  // Neither: only the allow-list rule
  await page.goto("/");
  await page.getByRole("button", { name: /alarm/i }).first().click();
  const inbox = page.getByRole("list", { name: "Alarms" });
  await expect(inbox.getByRole("button", { name: /^Gate plates: Plate KT55QRS on Camera 001/ })).toBeVisible();
  await expect(inbox.getByRole("button", { name: /^Strangers: Plate KT55QRS on Camera 001/ })).toBeVisible();
  await expect(inbox.getByRole("button", { name: /^Strangers: Plate XX00XXX on Camera 001/ })).toBeVisible();
  await expect(inbox.getByRole("button", { name: /Plate AB12CDE/ })).toHaveCount(0);
  await expect(inbox.getByRole("button", { name: /^Gate plates: Plate XX00XXX/ })).toHaveCount(0);
});

test("a plate is found in full or in part, look-alikes included, and a result opens Playback at that moment", async ({ page }) => {
  seedFixtures(2);
  seedPlates();
  await signInNext(page);
  await page.goto("/playback");
  await page.getByRole("button", { name: "Find a plate…" }).click();
  await expect(panel(page)).toBeVisible();
  const search = async text => {
    await panel(page).getByRole("searchbox", { name: /^Plate/ }).fill(text);
    await panel(page).getByRole("button", { name: "Search plates" }).click();
  };

  await search("AB12CDE");  // The exact plate, and the same plate read with a Z for the 2
  await expect(cards(page)).toHaveCount(2);
  await expect(cards(page).nth(0)).toContainText("AB12CDE");
  await expect(cards(page).nth(0)).toContainText("93% confident");
  await expect(cards(page).nth(1)).toContainText("AB1ZCDE");
  await expect(results(page).locator("img").first()).toHaveJSProperty("complete", true);

  await panel(page).getByRole("checkbox", { name: /look-alike/ }).uncheck();
  await panel(page).getByRole("button", { name: "Search plates" }).click();
  await expect(cards(page)).toHaveCount(1);
  await panel(page).getByRole("checkbox", { name: /look-alike/ }).check();

  await search("55q");  // Part of a plate
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("KT55QRS");
  await search("*ZZZ");  // A pattern: ends with
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("ZZ99ZZZ");
  await search("QQ");
  await expect(panel(page)).toContainText("No plate read matched.");

  await search("KT55");
  await cards(page).first().click();
  await expect(page.getByRole("region", { name: "Playback" })).toBeVisible();
});
