// Real-browser journeys for object tracks in Playback (#128). Camera 1 records 0-60 s from the anchor; detections were stored for the
// frames analysed at 4, 6, 8, 10, 12, 14 and 16 s (see seed_overlay.py): a person seen at 4, 6, 8, 14 and 16 s (hidden at 10 and 12 s) and a
// parked vehicle in every frame.
const { test, expect, seedFixtures, seedOverlayTracks, signInNext } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const tile = page => page.getByRole("group", { name: /^Camera 001.*: playback$/ });
const controls = page => page.getByRole("toolbar", { name: "Playback controls" });
const overlay = page => tile(page).getByRole("group", { name: "Detections" });
const boxes = page => overlay(page).locator(".track-box");

async function clockTime(page) {
  return Number(await page.getByLabel("Playback time").getAttribute("data-time"));
}

async function seekBy(page, seconds) {
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  for (let step = 0; step < Math.abs(seconds) / 5; step += 1) await ruler.press(seconds < 0 ? "ArrowLeft" : "ArrowRight");
}

async function open(page) {
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  const seeded = seedOverlayTracks(fixtures.footage.anchor);
  await signInNext(page);
  await nav(page).getByRole("link", { name: "Playback" }).click();
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => clockTime(page)).toBe(anchor);
  await expect(tile(page)).toHaveAttribute("data-phase", "ready");
  return { anchor, seeded };
}

/** The analysed frame (seconds from the anchor) a viewer should be shown at this offset, or null before the first. */
function expectedFrame(frames, offset) {
  const earlier = frames.filter(frame => frame <= offset);
  return earlier.length ? earlier[earlier.length - 1] : null;
}

test("boxes are off by default, toggle from the keyboard, and are not remembered", async ({ page }) => {
  await open(page);
  const toggle = controls(page).getByRole("button", { name: "Boxes" });
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await expect(overlay(page)).toHaveCount(0);

  await page.keyboard.press("b");
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(overlay(page)).toBeVisible();
  await page.keyboard.press("b");
  await expect(overlay(page)).toHaveCount(0);

  await toggle.click();
  await expect(overlay(page)).toBeVisible();
  await page.reload();
  await expect(toggle).toHaveAttribute("aria-pressed", "false"); // Boxes are off again on the next visit
  await expect(overlay(page)).toHaveCount(0);
});

test("boxes follow the stored detections of the shared clock at 1x and while frame stepping", async ({ page }) => {
  const { anchor, seeded } = await open(page);
  await page.keyboard.press("b");
  await expect(overlay(page)).toBeVisible();

  // Before the first analysed frame (4 s) nothing is drawn: boxes come only from stored detections.
  await expect(boxes(page)).toHaveCount(0);
  await seekBy(page, 5);
  // 5 s: the frame analysed at 4 s holds a person and the vehicle.
  await expect(boxes(page)).toHaveCount(2);
  const person = page.locator(`.track-box[data-track-id="${seeded.person}"]`);
  await expect(person).toHaveAttribute("data-at", String(anchor + 4000));
  const left = async () => parseFloat(await person.evaluate(element => element.style.left));
  expect(await left()).toBeCloseTo(seeded.person_x["4"] * 100, 1);

  // Stepping frame by frame (15 fps) across the 6 s analysed frame: at every step the boxes are the latest analysed frame at or before the clock.
  const next = controls(page).getByRole("button", { name: "Next frame" });
  const seen = [];
  await tile(page).click();
  for (let step = 0; step < 18; step += 1) {
    const before = await clockTime(page);
    await next.click();
    await expect.poll(() => clockTime(page)).toBeGreaterThan(before);
    const time = await clockTime(page);
    await expect.poll(async () => (await boxes(page).first().getAttribute("data-at")) ?? "none").toBe(String(anchor + 1000 * expectedFrame(seeded.frames, (time - anchor) / 1000)));
    seen.push(Number(await boxes(page).first().getAttribute("data-at")));
  }
  expect(new Set(seen).size).toBe(2); // It crossed from the 4 s frame to the 6 s frame, and only there
  expect(seen[0]).toBe(anchor + 4000);
  expect(seen[seen.length - 1]).toBe(anchor + 6000);
  expect(await left()).toBeCloseTo(seeded.person_x["6"] * 100, 1);

  // At 1x: play from here, stop once the clock has passed the 8 s frame, and the boxes are that frame's.
  await controls(page).getByRole("button", { name: "Play" }).click();
  await expect.poll(() => clockTime(page), { timeout: 15_000 }).toBeGreaterThan(anchor + 8_200);
  await controls(page).getByRole("button", { name: "Pause" }).click();
  const paused = await clockTime(page);
  await expect.poll(async () => (await boxes(page).first().getAttribute("data-at")) ?? "none").toBe(String(anchor + 1000 * expectedFrame(seeded.frames, (paused - anchor) / 1000)));
  expect(await left()).toBeCloseTo(seeded.person_x["8"] * 100, 1);

  // While the person is hidden (the 10 s frame) only the vehicle is drawn.
  await seekBy(page, 5);
  const hidden = await clockTime(page);
  const frame = expectedFrame(seeded.frames, (hidden - anchor) / 1000);
  await expect(boxes(page)).toHaveCount(seeded.person_at.includes(frame) ? 2 : 1);
  if (!seeded.person_at.includes(frame)) await expect(boxes(page).first()).toHaveAttribute("data-label", "vehicle");
});

test("following an object draws its stored path as a fading trail, across a pause", async ({ page }) => {
  const { seeded } = await open(page);
  await page.keyboard.press("b");
  await seekBy(page, 10); // 10 s: the person is hidden, the 10 s frame holds the vehicle
  await expect(boxes(page)).toHaveCount(1);
  await seekBy(page, -5); // 5 s
  const person = page.locator(`.track-box[data-track-id="${seeded.person}"]`);
  await expect(person).toBeVisible();
  await person.click();
  await expect(person).toHaveAttribute("aria-pressed", "true");
  await expect(tile(page)).toHaveAttribute("data-active", "true");

  // At 5 s: the points at 4 s only, so no segment yet. At 9 s the points at 4, 6 and 8 s make two segments through the stored centres.
  const trail = overlay(page).locator("svg.track-trail");
  await expect(trail).toHaveCount(0);
  await seekBy(page, 5); // 10 s ... the person is hidden but the path so far is still drawn
  const segments = trail.locator("line");
  await expect(segments).toHaveCount(2);
  const xs = await segments.evaluateAll(lines => lines.map(line => Number(line.dataset.x)));
  const centre = at => seeded.person_x[String(at)] + 0.05;
  const ys = await segments.evaluateAll(lines => lines.map(line => Number(line.dataset.y)));
  expect(ys).toEqual([ys[0], ys[0]]);
  expect(ys[0]).toBeCloseTo(0.5, 3); // The person's box is 0.3 to 0.7 high, so its centre is halfway down the picture
  expect(xs[0]).toBeCloseTo(centre(6), 3);
  expect(xs[1]).toBeCloseTo(centre(8), 3);
  const opacities = await segments.evaluateAll(lines => lines.map(line => Number(line.getAttribute("stroke-opacity"))));
  expect(opacities[0]).toBeLessThan(opacities[1]); // Older is fainter

  // After the pause the trail is bridged: at 15 s the stored points at 8 and 14 s are joined, up to 14 s.
  await seekBy(page, 5);
  await expect(segments).toHaveCount(3);
  const later = await segments.evaluateAll(lines => lines.map(line => Number(line.dataset.x)));
  expect(later[2]).toBeCloseTo(centre(14), 3);

  // Choosing it again stops following it.
  await seekBy(page, -10); // 5 s again, where the person is drawn
  await expect(person).toBeVisible();
  await person.click();
  await expect(person).toHaveAttribute("aria-pressed", "false");
  await expect(overlay(page).locator("svg.track-trail")).toHaveCount(0);
});

test("previous and next appearance land on the object's stored detections, across an occlusion", async ({ page }) => {
  const { anchor, seeded } = await open(page);
  await page.keyboard.press("b");
  await seekBy(page, 5);
  await page.locator(`.track-box[data-track-id="${seeded.person}"]`).click();
  const group = controls(page).getByRole("group", { name: "Object tracks" });
  const nextObject = group.getByRole("button", { name: /Object ▶/ });
  const previousObject = group.getByRole("button", { name: /◀ Object/ });

  for (const expected of [6, 8, 14, 16]) {
    await nextObject.click();
    await expect.poll(() => clockTime(page)).toBe(anchor + expected * 1000); // 8 to 14 s crosses the pause at 10 and 12 s
  }
  await nextObject.click();
  await expect(page.getByText("No later appearance of this object.")).toBeVisible();
  expect(await clockTime(page)).toBe(anchor + 16_000);

  await previousObject.click();
  await expect.poll(() => clockTime(page)).toBe(anchor + 14_000);
  await page.keyboard.press("[");
  await expect.poll(() => clockTime(page)).toBe(anchor + 8_000);
  await page.keyboard.press("]");
  await expect.poll(() => clockTime(page)).toBe(anchor + 14_000);

  await group.getByRole("button", { name: "Stop following" }).click();
  await expect(group.getByRole("button", { name: /Object ▶/ })).toHaveCount(0);
});
