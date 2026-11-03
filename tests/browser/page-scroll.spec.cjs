// Text pages (Insights, Digests, Cases) are longer than the window and must scroll to their end (#214).
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

for (const [name, path] of [["Insights", "/insights"], ["Digests", "/digests"], ["Cases", "/cases"]]) {
  test(`${name} scrolls to its end in a short window`, async ({ page }) => {
    seedFixtures(1);
    await page.setViewportSize({ width: 1200, height: 260 });
    await signInNext(page);
    await page.goto(path);
    const main = page.locator("main#main");
    await expect(main.getByRole("heading", { level: 1 })).toBeVisible();
    // Taller than the window, so it has to scroll, and a real wheel over the page moves it.
    await expect.poll(() => main.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);  // Once its data has arrived
    await page.mouse.move(600, 150);
    await page.mouse.wheel(0, 5000);
    await expect.poll(() => main.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    // The end of the page is reached: the last thing on it is inside the window.
    // (A page that finishes loading after the first scroll gets longer, so keep scrolling until it stops moving.)
    await expect.poll(async () => {
      await page.mouse.wheel(0, 5000);
      return main.evaluate(element => Math.round(element.scrollHeight - element.clientHeight - element.scrollTop));
    }, { timeout: 15_000 }).toBeLessThanOrEqual(1);
    const last = await main.evaluate(element => {
      const page = element.firstElementChild;
      const bottom = page.lastElementChild.getBoundingClientRect().bottom;
      return bottom <= window.innerHeight + 1;
    });
    expect(last).toBe(true);
  });
}
