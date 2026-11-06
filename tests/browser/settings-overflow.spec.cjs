// Text and tables stay inside their cards on every Settings page (#215): nothing inside a card extends past its right edge unless it sits in a box that scrolls sideways.
const { test, expect, seedFixtures, signInNext, writeHistory } = require("./helpers.cjs");

const CATEGORIES = ["general", "appearance", "access", "remote", "privacy", "recording", "tiering", "capacity", "coverage", "audit", "hardening", "notifications", "digests", "events", "failover", "plates", "sites", "integrations", "updates", "diagnostics"];

/** Elements inside a card (field group) whose right edge is past the card's, with no sideways-scrolling box between them and the card. */
function overflowing() {
  const scrolls = element => ["auto", "scroll"].includes(getComputedStyle(element).overflowX);
  const found = [];
  for (const card of document.querySelectorAll(".settings-page .field-group")) {
    const edge = card.getBoundingClientRect().right;
    for (const element of card.querySelectorAll("*")) {
      const box = element.getBoundingClientRect();
      if (box.width === 0 || box.height === 0 || box.right <= edge + 1) continue;
      let hidden = false;
      for (let parent = element.parentElement; parent && parent !== card; parent = parent.parentElement) {
        if (scrolls(parent) || getComputedStyle(parent).display === "none") { hidden = true; break; }
      }
      if (!hidden) found.push(`${element.tagName.toLowerCase()}${element.getAttribute("aria-label") ? `[${element.getAttribute("aria-label")}]` : ""} "${(element.textContent ?? "").trim().slice(0, 50)}" ends ${Math.round(box.right - edge)}px past its card`);
    }
  }
  return found.slice(0, 8);
}

test("no Settings page lets text or a table run past its card", async ({ page }) => {
  test.setTimeout(180_000);
  seedFixtures(2);
  await signInNext(page);
  writeHistory(1, 20); // A camera that will not keep its policy: long sentences in the retention forecast.
  // An event source: its address is one long unbreakable line.
  const source = await page.request.post("/api/event-sources", { data: { name: "Door-controller-with-a-very-long-name-that-has-no-spaces-in-it-at-all-and-keeps-going-on", kind: "http", secret: "a-secret-long-enough-to-be-accepted" } });
  expect(source.status()).toBe(201);
  const problems = [];
  for (const [width, category] of CATEGORIES.flatMap(name => [[1100, name], [420, name]])) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`/settings/${category}`);
    await expect(page.locator(".settings-page")).toBeVisible();
    if (category === "capacity") {
      await page.getByLabel("Cameras in row 1").fill("10");
      await expect(page.locator('tr[data-metric="disk_footage"]')).toBeVisible({ timeout: 15_000 });
    }
    if (category === "recording") {
      await page.getByRole("tab", { name: "Forecast" }).click();
      await expect(page.getByRole("table", { name: "Retention forecast by camera" })).toBeVisible({ timeout: 15_000 });
    }
    await page.waitForTimeout(300);
    for (const item of await page.evaluate(overflowing)) problems.push(`${category} at ${width}px: ${item}`);
  }
  expect(problems).toEqual([]);
});
