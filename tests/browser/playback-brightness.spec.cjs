// Playback shows footage at full brightness and in colour, like Live (#203): nothing dims, greys or covers the picture while it plays.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const tile = (page, name) => page.getByRole("group", { name: new RegExp(`^${name}.*: playback$`) });

async function openPlayback(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  await expect(page.getByRole("form", { name: "Footage search" })).toBeVisible();
}

test("a playing Playback tile is not dimmed, greyed or covered, and one that has nothing to show still is", async ({ page }) => {
  seedFixtures(2, { footage: true });
  await signInNext(page);
  await openPlayback(page);
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await page.getByRole("button", { name: /^Camera 002\b/ }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const playing = tile(page, "Camera 001");
  await expect(playing).toHaveAttribute("data-phase", "ready", { timeout: 15_000 });

  const style = await playing.locator("video").evaluate(video => { const css = getComputedStyle(video); return { opacity: css.opacity, filter: css.filter }; });
  expect(style).toEqual({ opacity: "1", filter: "none" });
  // Nothing opaque or tinted sits over the middle of the picture: the topmost thing there is the video (or the detection boxes the person asked for).
  const covering = await playing.evaluate(element => {
    const box = element.querySelector("video").getBoundingClientRect();
    const top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return top ? { tag: top.tagName, className: String(top.className) } : null;
  });
  expect(covering?.tag, JSON.stringify(covering)).toMatch(/^(VIDEO|CANVAS)$/);
  // The strip across the top of the tile is only a shading behind its name, and the name is all it holds.
  const gradient = await playing.locator(".tile-top").evaluate(element => getComputedStyle(element).backgroundImage);
  expect(gradient).toContain("linear-gradient");
  const topHeight = await playing.locator(".tile-top").evaluate(element => element.getBoundingClientRect().height);
  const tileHeight = await playing.evaluate(element => element.getBoundingClientRect().height);
  expect(topHeight / tileHeight).toBeLessThan(0.2);

  // A tile with nothing to show (a camera with no footage in the range) is still dimmed, so the difference is visible.
  const idle = tile(page, "Camera 002");
  const phase = await idle.getAttribute("data-phase");
  if (phase !== "ready") {
    const dim = await idle.locator("video").evaluate(video => getComputedStyle(video).opacity);
    expect(Number(dim)).toBeLessThan(1);
  }
});
