// Fisheye dewarping (#146): the WebGL stage draws each mode with straight lines and the same geometry an export uses, its views move by keyboard, mouse and touch, and a Live tile
// keeps its views' pan and zoom in saved views.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { test, expect, seedFixtures, signInNext, FIXTURE_PASSWORD } = require("./helpers.cjs");
const { loadFixtureEnvironment } = require("./fixture_environment.cjs");

const WEB = path.resolve(__dirname, "..", "..", "web");
const LENS = { mount: "ceiling", center_x: 0.5, center_y: 0.5, radius: 0.5, fov: 180 };
const WIDTH = 1280;
const HEIGHT = 720;

let bundle;
let directory;

function fixture(...args) {
  const out = path.join(directory, `${args.slice(1).join("_").replace(/[^a-z0-9_.-]/gi, "")}.png`);
  const result = spawnSync("python", ["-m", "tests.browser.dewarp_fixture", args[0], out, ...args.slice(1)], { cwd: process.cwd(), env: { ...process.env, ...loadFixtureEnvironment() }, encoding: "utf8", timeout: 120_000 });
  if (result.status !== 0) throw new Error(`dewarp fixture failed: ${result.stderr}`);
  return out;
}

test.beforeAll(async () => {
  directory = fs.mkdtempSync(path.join(os.tmpdir(), "nvr-dewarp-"));
  const esbuild = require(path.join(WEB, "node_modules", "esbuild"));
  const built = await esbuild.build({
    entryPoints: [path.resolve(__dirname, "dewarp_harness.tsx")], bundle: true, write: false, format: "iife", jsx: "automatic", platform: "browser",
    alias: { "@": path.join(WEB, "src") }, nodePaths: [path.join(WEB, "node_modules")], define: { "process.env.NODE_ENV": '"production"' },
  });
  bundle = built.outputFiles[0].text;
});

test.afterAll(() => { fs.rmSync(directory, { recursive: true, force: true }); });

/** Open the harness with the fixture fisheye picture and a state; resolves once the stage has drawn. */
async function open(page, state, size = { width: WIDTH, height: HEIGHT }, source = "image") {
  const picture = fixture("frame");
  await page.setViewportSize({ width: size.width + 40, height: size.height + 40 });
  const styles = fs.readFileSync(path.join(WEB, "src", "styles", "app.css"), "utf8");
  const dewarpCss = styles.slice(styles.indexOf("/* Fisheye dewarping (#146)"));
  await page.setContent(`<style>${dewarpCss}</style><body style="margin:0;background:#222"><img id="fixture" src="data:image/png;base64,${fs.readFileSync(picture).toString("base64")}" style="display:none"><div id="root"></div></body>`);
  await page.waitForFunction(() => document.getElementById("fixture").complete);
  await page.addScriptTag({ content: bundle });
  await page.evaluate(options => window.dewarpHarness.mount(options), { lens: LENS, state, ...size, source });
  await expect(page.getByTestId("dewarp-stage")).toBeVisible();
  await page.waitForTimeout(source === "video" ? 600 : 150);
}

/** The stage's canvas pixels as grey values (rows of numbers), read through a 2D canvas. */
async function pixels(page) {
  return page.evaluate(() => {
    const source = document.querySelector(".dewarp-canvas");
    const copy = document.createElement("canvas");
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext("2d", { willReadFrequently: true });
    context.drawImage(source, 0, 0);
    const { data, width, height } = context.getImageData(0, 0, copy.width, copy.height);
    const grey = new Float32Array(width * height);
    for (let i = 0; i < width * height; i += 1) grey[i] = (data[i * 4] + data[i * 4 + 1] + data[i * 4 + 2]) / 3;
    return { width, height, grey: Array.from(grey) };
  });
}

/** Centres of whole dark runs along a line of grey values. */
function darkCentres(line) {
  const centres = [];
  let i = 0;
  while (i < line.length) {
    if (255 - line[i] > 100) {
      let j = i;
      while (j < line.length && 255 - line[j] > 100) j += 1;
      if (i > 0 && j < line.length) {
        let weight = 0;
        let sum = 0;
        for (let k = i; k < j; k += 1) { weight += 255 - line[k]; sum += (255 - line[k]) * k; }
        centres.push(sum / weight);
      }
      i = j;
    } else i += 1;
  }
  return centres;
}

const row = (image, y) => image.grey.slice(y * image.width, (y + 1) * image.width);
const column = (image, x) => Array.from({ length: image.height }, (_, y) => image.grey[y * image.width + x]);
const spread = (lists, count) => {
  const usable = lists.filter(list => list.length === count);
  expect(usable.length, `lines found per sample: ${lists.map(list => list.length)}`).toBeGreaterThanOrEqual(2);
  return Math.max(...Array.from({ length: count }, (_, k) => Math.max(...usable.map(list => list[k])) - Math.min(...usable.map(list => list[k]))));
};

test("a panorama shows the room's vertical lines and horizontal bands straight", async ({ page }) => {
  await open(page, { mode: "panorama", pan: 0, views: [] });
  const image = await pixels(page);
  expect(image.width).toBe(WIDTH);
  // The strip keeps its own 4:1 shape in the 16:9 tile: 720 px wide drawn 1280 wide, black bars above and below.
  const top = Math.round((image.height - image.width / 4) / 2);
  const rows = [0.05, 0.15, 0.25, 0.35].map(fraction => Math.round(top + (image.width / 4) * fraction));
  const stripes = rows.map(y => darkCentres(row(image, y)));
  expect(stripes[0].length).toBe(23);
  expect(spread(stripes, 23)).toBeLessThan(2.5);
});

test("a playing video of the fisheye picture dewarps straight too", async ({ page }) => {
  // The path a Live tile takes: a video element, redrawn into the texture as it plays.
  await open(page, { mode: "views", pan: 0, views: [{ pan: 0, tilt: -90, fov: 90 }] }, { width: WIDTH, height: HEIGHT }, "video");
  const image = await pixels(page);
  const verticals = [20, 150, 260, 460, 560, 700].map(y => darkCentres(row(image, y)));
  expect(verticals[0].length).toBeGreaterThanOrEqual(9);
  expect(spread(verticals, verticals[0].length)).toBeLessThan(2.5);
});

test("a double panorama shows two straight half circles", async ({ page }) => {
  await open(page, { mode: "double_panorama", pan: 0, views: [] });
  const image = await pixels(page);
  const half = image.height / 2;
  const topRows = [0.06, 0.2, 0.33].map(fraction => darkCentres(row(image, Math.round(half * fraction))));
  const bottomRows = [0.06, 0.2, 0.33].map(fraction => darkCentres(row(image, Math.round(half + half * fraction))));
  expect(topRows[0].length).toBe(11);
  expect(bottomRows[0].length).toBe(11);
  expect(spread(topRows, 11)).toBeLessThan(2.5);
  expect(spread(bottomRows, 11)).toBeLessThan(2.5);
});

test("a virtual view looking down shows the floor grid straight, evenly spaced", async ({ page }) => {
  await open(page, { mode: "views", pan: 0, views: [{ pan: 0, tilt: -90, fov: 90 }] });
  const image = await pixels(page);
  const verticals = [20, 150, 260, 460, 560, 700].map(y => darkCentres(row(image, y)));
  expect(spread(verticals, verticals[0].length)).toBeLessThan(2.5);
  expect(verticals[0].length).toBeGreaterThanOrEqual(9);
  const gaps = verticals[0].slice(1).map((value, k) => value - verticals[0][k]).slice(1, -1);
  expect(Math.max(...gaps) - Math.min(...gaps)).toBeLessThan(2.5);
  const horizontals = [60, 190, 320, 450, 580, 700, 830, 960, 1090].map(x => darkCentres(column(image, x)));
  const most = Math.max(...horizontals.map(list => list.length));
  expect(spread(horizontals, most)).toBeLessThan(2.5);
});

for (const view of [{ mode: "view", pan: 20, tilt: -60, fov: 80 }, { mode: "panorama", pan: 30, tilt: 0, fov: 0 }, { mode: "double_panorama", pan: 0, tilt: 0, fov: 0 }]) {
  test(`the ${view.mode.replace("_", " ")} on screen matches the reference geometry and the export's rendering`, async ({ page }) => {
    const aspect = { view: [1280, 720], panorama: [1920, 480], double_panorama: [1280, 720] }[view.mode];
    // A tile of exactly the export's shape, so the picture fills it.
    await open(page, view.mode === "view" ? { mode: "views", pan: 0, views: [{ pan: view.pan, tilt: view.tilt, fov: view.fov }] } : { mode: view.mode, pan: view.pan, views: [] },
      { width: aspect[0], height: aspect[1] });
    const reference = fixture("reference", view.mode, view.pan, view.tilt, view.fov || 90);
    const exported = fixture("export", view.mode, view.pan, view.tilt, view.fov || 90);
    for (const [name, file, limit, brightLimit] of [["reference", reference, 5, 0.02], ["export", exported, 6, 0.03]]) {
      const result = await page.evaluate(async source => {
        const picture = new Image();
        picture.src = source;
        await picture.decode();
        const grab = element => {
          const copy = document.createElement("canvas");
          copy.width = element.width;
          copy.height = element.height;
          const context = copy.getContext("2d", { willReadFrequently: true });
          context.drawImage(element, 0, 0);
          return context.getImageData(0, 0, copy.width, copy.height).data;
        };
        const screen = grab(document.querySelector(".dewarp-canvas"));
        const other = grab(picture);
        if (screen.length !== other.length) return { size: [document.querySelector(".dewarp-canvas").width, picture.width] };
        let total = 0;
        let bright = 0;
        for (let i = 0; i < screen.length; i += 4) {
          const d = (Math.abs(screen[i] - other[i]) + Math.abs(screen[i + 1] - other[i + 1]) + Math.abs(screen[i + 2] - other[i + 2])) / 3;
          total += d;
          if (d > 64) bright += 1;
        }
        return { mean: total / (screen.length / 4), bright: bright / (screen.length / 4) };
      }, `data:image/png;base64,${fs.readFileSync(file).toString("base64")}`);
      expect(result.size, `${name}: sizes differ`).toBeUndefined();
      expect(result.mean, `${name} mean difference`).toBeLessThan(limit);
      expect(result.bright, `${name} share of clearly different pixels`).toBeLessThan(brightLimit);
    }
  });
}

test.describe("controls", () => {
  const region = page => page.getByRole("group", { name: "Dome: Virtual view 1 of 1" });

  test("a view turns and zooms by keyboard", async ({ page }) => {
    await open(page, { mode: "views", pan: 0, views: [{ pan: 0, tilt: -45, fov: 90 }] });
    const view = region(page);
    await view.focus();
    await expect(view).toHaveAttribute("data-pan", "0.0");
    await page.keyboard.press("ArrowLeft");
    await expect(view).toHaveAttribute("data-pan", "5.0"); // Looking left on a ceiling mount: the azimuth grows
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(view).toHaveAttribute("data-pan", "-5.0");
    await page.keyboard.press("ArrowUp");
    await expect(view).toHaveAttribute("data-tilt", "-40.0");
    await page.keyboard.press("Shift+ArrowDown");
    await expect(view).toHaveAttribute("data-tilt", "-60.0");
    await page.keyboard.press("+");
    expect(Number(await view.getAttribute("data-fov"))).toBeCloseTo(76.5, 1);
    await page.keyboard.press("-");
    expect(Number(await view.getAttribute("data-fov"))).toBeCloseTo(90, 0);
    for (let i = 0; i < 6; i += 1) await page.keyboard.press("-");
    await expect(view).toHaveAttribute("data-fov", "120.0"); // Held to the widest view
    for (let i = 0; i < 40; i += 1) await page.keyboard.press("ArrowUp");
    await expect(view).toHaveAttribute("data-tilt", "0.0"); // And a ceiling camera cannot look above the horizon
    expect((await page.evaluate(() => window.dewarpHarness.state())).views[0].tilt).toBe(0);
  });

  test("a view turns by dragging and zooms by the wheel, and each view of several keeps its own", async ({ page }) => {
    await open(page, { mode: "views", pan: 0, views: [{ pan: 0, tilt: -45, fov: 90 }, { pan: 120, tilt: -30, fov: 60 }] });
    const first = page.getByRole("group", { name: "Dome: Virtual view 1 of 2" });
    const second = page.getByRole("group", { name: "Dome: Virtual view 2 of 2" });
    const box = await first.boundingBox();
    // Dragging right by a quarter of the view's width grabs the picture: a ceiling camera turns towards the larger azimuth by a quarter of the field of view.
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.65, box.y + box.height * 0.5, { steps: 5 });
    await page.mouse.up();
    expect(Number(await first.getAttribute("data-pan"))).toBeCloseTo(22.5, 0);
    await expect(second).toHaveAttribute("data-pan", "120.0");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -200); // Away from the person: zoom in
    await expect.poll(async () => Number(await first.getAttribute("data-fov"))).toBeLessThan(75);
    await expect(second).toHaveAttribute("data-fov", "60.0");
    const state = await page.evaluate(() => window.dewarpHarness.state());
    expect(state.views.map(view => Math.round(view.pan))).toEqual([23, 120]);
  });

  test("by touch: one finger drags, two fingers pinch", async ({ page }) => {
    await open(page, { mode: "views", pan: 0, views: [{ pan: 0, tilt: -45, fov: 90 }] });
    const view = region(page);
    const box = await view.boundingBox();
    const fire = (type, id, x, y) => view.evaluate((element, args) => element.dispatchEvent(new PointerEvent(args.type, {
      pointerId: args.id, pointerType: "touch", isPrimary: args.id === 1, clientX: args.x, clientY: args.y, bubbles: true, cancelable: true })), { type, id, x, y });
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    await fire("pointerdown", 1, cx, cy);
    await fire("pointermove", 1, cx - box.width * 0.2, cy);
    await fire("pointerup", 1, cx - box.width * 0.2, cy);
    expect(Number(await view.getAttribute("data-pan"))).toBeCloseTo(-18, 0); // Dragging left turns the other way
    // Pinch: two fingers 200 px apart move to 100 px apart (a pinch in zooms out: the field of view widens).
    await fire("pointerdown", 1, cx - 100, cy);
    await fire("pointerdown", 2, cx + 100, cy);
    await fire("pointermove", 2, cx + 50, cy);
    await fire("pointermove", 1, cx - 50, cy);
    await fire("pointerup", 1, cx - 50, cy);
    await fire("pointerup", 2, cx + 50, cy);
    expect(Number(await view.getAttribute("data-fov"))).toBeGreaterThan(110);
    // Spreading zooms in.
    await fire("pointerdown", 1, cx - 40, cy);
    await fire("pointerdown", 2, cx + 40, cy);
    await fire("pointermove", 2, cx + 120, cy);
    await fire("pointermove", 1, cx - 120, cy);
    await fire("pointerup", 1, cx - 120, cy);
    await fire("pointerup", 2, cx + 120, cy);
    expect(Number(await view.getAttribute("data-fov"))).toBeLessThan(80);
  });

  test("a panorama scrolls by keyboard and by dragging, and wraps round", async ({ page }) => {
    await open(page, { mode: "panorama", pan: 170, views: [] });
    const strip = page.getByRole("group", { name: "Dome: Panorama" });
    await strip.focus();
    await page.keyboard.press("ArrowLeft"); // Scroll left: the strip starts at a larger azimuth, past 180 it wraps
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    expect(Number(await strip.getAttribute("data-pan"))).toBeCloseTo(-175, 0);
    const box = await strip.boundingBox();
    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height / 2, { steps: 4 });
    await page.mouse.up();
    expect(Number(await strip.getAttribute("data-pan"))).toBeCloseTo(-175 + 36, 0); // Dragging right shows what was further left: the ceiling camera's azimuth grows
  });
});

test("without WebGL the stage reports it and draws nothing", async ({ page }) => {
  await open(page, { mode: "raw", pan: 0, views: [] }); // (the page is set up first: a stage mounted after the override finds no WebGL)
  await page.evaluate(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) { return /webgl/.test(String(type)) ? null : original.call(this, type, ...rest); };
    document.getElementById("root").innerHTML = "";
  });
  await page.evaluate(options => window.dewarpHarness.mount(options), { lens: LENS, state: { mode: "panorama", pan: 0, views: [] }, width: 640, height: 360 });
  await expect.poll(() => page.evaluate(() => window.dewarpHarness.unsupported())).toBe(true);
});

// --- in the Live workspace --------------------------------------------------------------------------------------------------------------

async function setLens(page, cameraId, body) {
  const response = await page.request.put(`/api/cameras/${cameraId}/lens`, { data: body });
  expect(response.status()).toBe(200);
}

test("a Live tile keeps each virtual view's pan and zoom in saved views and across a reload", async ({ page }) => {
  const manifest = seedFixtures(2);
  await signInNext(page);
  await setLens(page, manifest.camera_ids[0], { kind: "fisheye", mount: "ceiling", center_x: 0.5, center_y: 0.5, radius: 0.5, fov: 180 });
  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const tile = page.getByRole("group", { name: /^Camera 001:/ });
  await expect(tile).toBeVisible();
  await tile.getByLabel("Dewarp Camera 001").selectOption("2");
  const first = tile.getByRole("group", { name: /Virtual view 1 of 2/ });
  const second = tile.getByRole("group", { name: /Virtual view 2 of 2/ });
  await expect(first).toBeVisible();
  await first.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("+");
  await second.focus();
  await page.keyboard.press("Shift+ArrowUp");
  const before = [await first.getAttribute("data-pan"), await first.getAttribute("data-fov"), await second.getAttribute("data-tilt")];

  await page.getByRole("button", { name: "Save as…" }).click();
  await page.getByRole("dialog").getByLabel("Name", { exact: true }).fill("Dome views");
  await page.getByRole("button", { name: "Save view" }).click();
  await expect(page.getByText("My view")).toBeVisible();

  // Change the views, then load the saved view back: the saved pan and zoom return, not the changed ones.
  await first.focus();
  for (let i = 0; i < 5; i += 1) await page.keyboard.press("ArrowRight");
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  const changedPan = await first.getAttribute("data-pan");
  // The workspace is saved a moment after a change: wait until the server has the changed pan before reloading.
  await expect.poll(async () => String(((await (await page.request.get("/api/live/workspace")).json()).dewarp?.[manifest.camera_ids[0]]?.views?.[0]?.pan ?? "").toFixed?.(1))).toBe(changedPan);
  await page.reload();
  await expect(tile).toBeVisible();
  const restored = tile.getByRole("group", { name: /Virtual view 1 of 2/ });
  await expect(restored).toBeVisible(); // The workspace came back with its views as they were left
  await expect(restored).toHaveAttribute("data-pan", changedPan);
  await page.getByRole("tab", { name: "Views" }).click();
  await page.getByRole("button", { name: /Dome views/ }).click();
  await expect(restored).toHaveAttribute("data-pan", before[0]);
  await expect(restored).toHaveAttribute("data-fov", before[1]);
  await expect(tile.getByRole("group", { name: /Virtual view 2 of 2/ })).toHaveAttribute("data-tilt", before[2]);
});

test("without WebGL a fisheye tile says so and keeps the raw picture", async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) { return /webgl/.test(String(type)) ? null : original.call(this, type, ...rest); };
  });
  const manifest = seedFixtures(1);
  await signInNext(page);
  await setLens(page, manifest.camera_ids[0], { kind: "fisheye" });
  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const tile = page.getByRole("group", { name: /^Camera 001:/ });
  await tile.getByLabel("Dewarp Camera 001").selectOption("panorama");
  await expect(tile.getByTestId("tile-quality-note")).toContainText("Dewarping needs WebGL");
  await expect(tile.getByLabel("Dewarp Camera 001")).toHaveValue("raw");
});

test("an administrator sets a camera's lens", async ({ page }) => {
  const manifest = seedFixtures(1);
  await signInNext(page);
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "Lens (360°)" }).click();
  const form = page.getByRole("form", { name: "Lens" });
  await form.getByLabel("Camera type").selectOption("fisheye");
  await form.getByLabel("Mount").selectOption("wall");
  await form.getByLabel("Circle radius (% of the picture's height)").fill("47");
  await form.getByLabel("Field of view across the circle (degrees)").fill("185");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved. Live can dewarp this camera.")).toBeVisible();
  const saved = await (await page.request.get(`/api/cameras/${manifest.camera_ids[0]}/lens`)).json();
  expect(saved).toEqual({ kind: "fisheye", mount: "wall", center_x: 0.5, center_y: 0.5, radius: 0.47, fov: 185 });
  await form.getByLabel("Circle radius (% of the picture's height)").fill("5");
  await expect(form.getByRole("button", { name: "Save" })).toBeDisabled();
});

test("an export of a 360° camera can render a panorama or a virtual view", async ({ page }) => {
  test.setTimeout(240_000);
  const fixtures = seedFixtures(3, { footage: true });
  const anchor = Date.parse(fixtures.footage.anchor);
  await signInNext(page);
  await setLens(page, fixtures.camera_ids[0], { kind: "fisheye", mount: "ceiling", center_x: 0.5, center_y: 0.5, radius: 0.5, fov: 180 });
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Playback" }).click();
  for (const index of [1, 2]) await page.getByRole("button", { name: new RegExp(`^Camera ${String(index).padStart(3, "0")}\\b`) }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(async () => Number(await page.getByLabel("Playback time").getAttribute("data-time"))).toBe(anchor);
  const ruler = page.getByRole("slider", { name: "Playback position" });
  await ruler.focus();
  for (let step = 0; step < 6; step += 1) await ruler.press("ArrowRight");
  await page.getByRole("button", { name: "Mark in" }).click();
  for (let step = 0; step < 2; step += 1) await ruler.press("ArrowRight");
  await page.getByRole("button", { name: "Mark out" }).click();

  const drawer = page.getByRole("complementary", { name: "Export footage" });
  await expect(drawer).toBeVisible();
  // Only the 360° camera is offered a rendering; a normal camera is not.
  await expect(drawer.getByLabel("How Camera 001 is rendered")).toBeVisible();
  await expect(drawer.getByLabel("How Camera 002 is rendered")).toHaveCount(0);
  await drawer.getByLabel("How Camera 001 is rendered").selectOption("view");
  await drawer.getByLabel("Pan (°)").fill("30");
  await drawer.getByLabel("Tilt (°)").fill("-50");
  await drawer.getByLabel("Field of view (°)").fill("75");
  // A grid follows the raw circle; blurring and a view cannot be combined (blur is offered only when a model is installed).
  await drawer.getByRole("button", { name: "Export", exact: true }).click();
  await expect(drawer.getByText("Export queued.")).toBeVisible();
  const metadata = async () => {
    const jobs = (await (await page.request.get("/api/exports")).json()).exports ?? [];
    const finished = jobs.find(job => job.status === "completed");
    return finished ? finished : jobs[0];
  };
  await expect.poll(async () => (await metadata())?.status, { timeout: 200_000 }).toBe("completed");
  const job = await metadata();
  expect(job.dewarp).toEqual({ [fixtures.camera_ids[0]]: { mode: "view", pan: 30, tilt: -50, fov: 75 } });
  const camera = job.metadata.cameras.find(item => item.camera_id === fixtures.camera_ids[0]);
  expect(camera.dewarp).toMatchObject({ mode: "view", pan: 30, tilt: -50, fov: 75, output_size: [1280, 720] });
  expect(job.metadata.cameras.find(item => item.camera_id === fixtures.camera_ids[1]).dewarp).toBeUndefined();
});
