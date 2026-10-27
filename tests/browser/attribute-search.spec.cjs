// Real-browser journey for attribute search across cameras (#125).
const { test, expect, seedFixtures, signInNext, seedSearchTracks } = require("./helpers.cjs");

const panel = page => page.getByRole("complementary", { name: "Attribute search" });
const results = page => panel(page).getByRole("list", { name: "Search results" });
const cards = page => results(page).getByRole("button", { name: /^Open/ });

async function openSearch(page) {
  await page.goto("/playback");
  await page.getByRole("button", { name: "Find by attributes…" }).click();
  await expect(panel(page)).toBeVisible();
}

test("attribute search finds tracks by what they looked like, lists what was not analysed, and opens Playback at the moment", async ({ page }) => {
  seedFixtures(3);
  const tracks = seedSearchTracks();
  await signInNext(page);
  await openSearch(page);

  // Red tops, anywhere, in the last day: two people on two cameras, newest first.
  await panel(page).getByLabel("Kind").selectOption("person");
  await panel(page).getByLabel("Top colour").selectOption("red");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(2);
  await expect(cards(page).nth(0)).toContainText("Camera 003");
  await expect(cards(page).nth(1)).toContainText("Camera 001");
  await expect(cards(page).nth(1)).toContainText("red top, blue lower, moving right");
  await expect(results(page).getByRole("img")).toHaveCount(0); // The pictures are decorative: the card's name says what it is.
  await expect(results(page).locator("img").first()).toHaveJSProperty("complete", true);

  // The unanalysed time is listed above the results: camera 1's failed hour and camera 3, never analysed.
  const coverage = panel(page).getByRole("region", { name: "Analysis coverage" });
  await expect(coverage).toContainText("of the searched time was analysed");
  await expect(coverage).toContainText("not looked at, not empty");
  await coverage.getByText(/Not analysed: 2 cameras/).click();
  const gaps = coverage.getByRole("list", { name: "Unanalysed time by camera" });
  await expect(gaps).toContainText("Camera 001");
  await expect(gaps).toContainText("Analysis failed");
  await expect(gaps).toContainText("Camera stream ended");
  await expect(gaps).toContainText("Camera 003");
  await expect(gaps).toContainText("AI not running");
  await expect(gaps).not.toContainText("Camera 002");

  // Narrowing the filters narrows the results, and a combination nobody matched says so.
  await panel(page).getByLabel("Lower colour").selectOption("blue");
  await panel(page).getByLabel("Direction").selectOption("right");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("Camera 001");
  await panel(page).getByLabel("Direction").selectOption("away");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(panel(page)).toContainText("No matches in the time that was analysed.");

  // Vehicles: the type filter says why it cannot be used without the attribute model, and colour still works.
  await panel(page).getByRole("button", { name: "Clear" }).click();
  await panel(page).getByLabel("Kind").selectOption("vehicle");
  await expect(panel(page).getByLabel("Top colour")).toHaveCount(0);
  await expect(panel(page).getByLabel("Vehicle type")).toBeDisabled();
  await expect(panel(page)).toContainText("Needs the vehicle attribute model, which is not installed.");
  await panel(page).getByLabel("Vehicle colour").selectOption("red");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("Camera 002");
  await expect(cards(page)).toContainText("red truck, toward the camera");

  // Where in the picture: only the truck, driving down the frame, passed through its bottom half; the people walked across the upper part.
  await panel(page).getByRole("button", { name: "Clear" }).click();
  await panel(page).getByLabel("Passed through").selectOption("bottom");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("Camera 002");

  // A result opens Playback at its moment with its camera selected, by keyboard too.
  await panel(page).getByRole("button", { name: "Clear" }).click();
  await panel(page).getByLabel("Kind").selectOption("person");
  await panel(page).getByLabel("Top colour").selectOption("green");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await cards(page).first().focus();
  await page.keyboard.press("Enter");
  const tile = page.getByRole("group", { name: /^Camera 001.*: playback$/ });
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAttribute("data-active", "true");
  await expect.poll(async () => Number(await page.getByLabel("Playback time").getAttribute("data-time"))).toBe(Date.parse(tracks["green-walker"].first_seen_at));
});

test("an operator searches the cameras they may see", async ({ page }) => {
  seedFixtures(3);
  seedSearchTracks();
  await signInNext(page, "camera-operator");
  await openSearch(page);
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(4); // The operator fixture sees every camera, as before grants existed.
});

test("Find similar lists look-alikes on other cameras with how alike they are, and each opens Playback", async ({ page }) => {
  seedFixtures(3);
  const tracks = seedSearchTracks();
  await signInNext(page);
  await openSearch(page);
  await panel(page).getByLabel("Kind").selectOption("person");
  await panel(page).getByRole("button", { name: "Search", exact: true }).click();
  await expect(cards(page)).toHaveCount(3);

  // The red-over-blue walker on Camera 001: the same-looking person on Camera 003 is found, and the one in green is not.
  await results(page).getByRole("button", { name: /^Find similar to Camera 001.*red top, blue lower/ }).click();
  const alike = panel(page).getByRole("region", { name: "Similar objects" });
  await expect(alike).toContainText("Similar to Camera 001");
  const found = alike.getByRole("list", { name: "Similar results" }).getByRole("button");
  await expect(found).toHaveCount(1);
  await expect(found.first()).toContainText("Camera 003");
  await expect(found.first()).toContainText(/\d+% similar/);
  await expect(alike).toContainText("not proof they are the same");

  // Choosing a result opens Playback on that camera at that moment.
  await found.first().click();
  await expect(page.getByRole("group", { name: /^Camera 003.*: playback$/ })).toBeVisible();
  expect(tracks["far-walker"].camera_id).toBeTruthy();
});

test("describing what to look for finds it, says what was understood and which model ranked it, and opens Playback", async ({ page }) => {
  seedFixtures(3);
  seedSearchTracks();
  await signInNext(page);
  await openSearch(page);

  const box = panel(page).getByRole("searchbox", { name: /Describe what you are looking for/ });
  await box.fill("person in a red jacket after 12 am at camera 003");
  await panel(page).getByRole("button", { name: "Ask", exact: true }).click();
  const answer = panel(page).getByRole("region", { name: "Description results" });
  await expect(answer).toContainText("Cameras: Camera 003");
  await expect(answer).toContainText("Time of day: 00:00 to 24:00");
  await expect(answer).toContainText("No image-text model is installed");
  const matches = answer.getByRole("list", { name: "Description matches" }).getByRole("button");
  await expect(matches).toHaveCount(1);
  await expect(matches.first()).toContainText("Camera 003");
  await expect(matches.first()).toContainText("score 100");
  await expect(answer).toContainText("not analysed"); // Camera 003 was never analysed: the answer says so

  // Without a camera phrase the red tops on every camera are found, best first.
  await box.fill("red jacket");
  await panel(page).getByRole("button", { name: "Ask", exact: true }).click();
  await expect(answer.getByRole("list", { name: "Description matches" }).getByRole("button")).toHaveCount(2);

  await answer.getByRole("list", { name: "Description matches" }).getByRole("button").first().click();
  await expect(page.getByRole("group", { name: /^Camera 003.*: playback$/ })).toBeVisible();
});
