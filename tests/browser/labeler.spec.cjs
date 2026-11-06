// The frame labeler (#255): a single page opened from disk (it needs no server) for marking where the people are on pictures, to score the person detector.
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { test, expect } = require("./helpers.cjs");

/** A solid-colour PNG made by the page itself, as the bytes an <input type=file> would hand over. */
async function picture(page, name, width, height, color) {
  const base64 = await page.evaluate(async ([w, h, c]) => {
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const context = canvas.getContext("2d");
    context.fillStyle = c; context.fillRect(0, 0, w, h);
    const bytes = new Uint8Array(await (await new Promise(resolve => canvas.toBlob(resolve, "image/png"))).arrayBuffer());
    return btoa(String.fromCharCode(...bytes));
  }, [width, height, color]);
  return { name, mimeType: "image/png", buffer: Buffer.from(base64, "base64") };
}

async function drag(page, from, to) {
  const box = await page.locator("canvas").boundingBox();
  await page.mouse.move(box.x + box.width * from[0], box.y + box.height * from[1]);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * to[0], box.y + box.height * to[1], { steps: 5 });
  await page.mouse.up();
}

test("boxes are drawn, tagged and saved as fractions of the picture, and an empty picture is saved as nobody", async ({ page }) => {
  await page.goto(pathToFileURL(path.resolve("tools/frame_labeler.html")).href);
  await page.locator("#pictures").setInputFiles([await picture(page, "frame_00002.png", 400, 200, "#335577"), await picture(page, "frame_00001.png", 400, 200, "#775533")]);
  await expect(page.getByRole("status").first()).toContainText("1 of 2 · frame_00001.png · 0 people"); // Sorted by name
  await page.getByLabel("Tag for new boxes").selectOption("wheelchair");
  await drag(page, [0.2, 0.2], [0.5, 0.7]);
  await expect(page.getByRole("status").first()).toContainText("1 person");
  await expect(page.getByLabel("Tag of the selected box")).toHaveValue("wheelchair");
  await page.getByLabel("Tag for new boxes").selectOption("person");
  await drag(page, [0.6, 0.1], [0.9, 0.9]);
  await page.getByLabel("Tag of the selected box").selectOption("seated");
  await expect(page.getByRole("status").first()).toContainText("2 people");
  // Another picture, looked at and left empty; going back finds the boxes where they were.
  await page.getByRole("button", { name: "Next ▶" }).click();
  await expect(page.getByRole("status").first()).toContainText("2 of 2 · frame_00002.png · 0 people");
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByRole("status").first()).toContainText("frame_00001.png · 2 people");
  // A click chooses a box and Delete removes it.
  const canvas = await page.locator("canvas").boundingBox();
  await page.mouse.click(canvas.x + canvas.width * 0.35, canvas.y + canvas.height * 0.45);
  await expect(page.getByLabel("Tag of the selected box")).toHaveValue("wheelchair");
  await page.getByRole("button", { name: "Delete selected box" }).click();
  await expect(page.getByRole("status").first()).toContainText("1 person");
  await drag(page, [0.1, 0.1], [0.3, 0.6]);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Save labels.json" }).click();
  const saved = JSON.parse(require("node:fs").readFileSync(await (await download).path(), "utf8"));
  expect(Object.keys(saved.frames)).toEqual(["frame_00001.png", "frame_00002.png"]);
  expect(saved.frames["frame_00002.png"]).toEqual([]); // Looked at, nobody there
  const [seated, wheelchair] = saved.frames["frame_00001.png"];
  expect(seated.tag).toBe("seated");
  expect(seated.box[0]).toBeCloseTo(0.6, 1);
  expect(seated.box[3]).toBeCloseTo(0.8, 1);
  expect(wheelchair.tag).toBe("person");
  expect(wheelchair.box[2]).toBeCloseTo(0.2, 1);
});
