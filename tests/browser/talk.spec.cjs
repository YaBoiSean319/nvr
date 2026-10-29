// Two-way audio (#147): an administrator turns talk on for a camera and lets operators use it; holding Talk on the Live tile sends the microphone to the camera's
// speaker, releasing stops it at once, the microphone is asked for only on the first talk, a second person is told who is talking, and talks are listed.
const { test, expect, seedFixtures, signInNext, signInAs } = require("./helpers.cjs");
const { startTalkCamera } = require("./talk_camera.cjs");

let camera;
test.beforeEach(async () => { camera = await startTalkCamera(); });
test.afterEach(async () => { await camera.close(); });

// A microphone that plays a tone, and a count of how often the page asked for one.
const FAKE_MICROPHONE = () => {
  window.__microphoneRequests = 0;
  navigator.mediaDevices.getUserMedia = async () => {
    window.__microphoneRequests += 1;
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const destination = context.createMediaStreamDestination();
    oscillator.connect(destination);
    oscillator.start();
    return destination.stream;
  };
};

test("hold to talk reaches the camera, release stops it, and a second person sees who is talking", async ({ page, browser, baseURL }) => {
  await page.addInitScript(FAKE_MICROPHONE);
  seedFixtures(2, { streams: { 1: camera.url } });
  await signInNext(page);

  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "Talk" }).click();
  await expect(page.getByText("Nobody has talked through this camera yet.")).toBeVisible();
  await page.getByLabel("Allow talking through this camera").check();
  await page.getByRole("group", { name: "Who may talk" }).getByLabel("Operators").check();
  await page.getByRole("button", { name: /^Save/ }).click();
  await expect(page.getByText("Saved. Talk is on for this camera.")).toBeVisible();

  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const talk = page.getByRole("button", { name: "Talk through Camera 001" });
  await expect(talk).toBeVisible();
  expect(await page.evaluate(() => window.__microphoneRequests)).toBe(0); // Not asked for until the first talk

  const box = await talk.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await expect(talk).toHaveAttribute("data-phase", "talking");
  await expect(page.getByRole("status").filter({ hasText: "You are talking" })).toBeVisible();
  expect(await page.evaluate(() => window.__microphoneRequests)).toBe(1);
  await expect.poll(() => camera.packets.length, { timeout: 10_000 }).toBeGreaterThan(5);
  expect(new Set(camera.packets.map(packet => packet.payloadType))).toEqual(new Set([0]));

  // While it is held, an operator sees who is talking and cannot take over.
  const operator = await signInAs(browser, baseURL, "camera-operator");
  await operator.page.goto("/live");
  await operator.page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const theirs = operator.page.getByRole("button", { name: "Talk through Camera 001" });
  await expect(theirs).toBeDisabled({ timeout: 15_000 });
  await expect(theirs).toContainText("camera-admin is talking");

  await page.mouse.up();
  await expect(talk).toHaveAttribute("data-phase", "idle");
  await expect.poll(() => camera.state.teardowns).toBe(1);
  const delivered = camera.packets.length;
  await page.waitForTimeout(600);
  expect(camera.packets.length).toBe(delivered); // Nothing after release
  await expect(theirs).toBeEnabled({ timeout: 15_000 });
  await operator.context.close();

  // The talk is in the camera's history, and the microphone was not asked for again by merely looking.
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "Talk" }).click();
  await expect(page.getByRole("region", { name: "Talk history" })).toContainText("camera-admin");
});

test("a camera with talk off shows no talk control", async ({ page }) => {
  seedFixtures(2, { streams: { 1: camera.url } });
  await signInNext(page);
  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await expect(page.locator(".tile[data-camera-id]")).toBeVisible();
  await expect(page.getByRole("button", { name: "Talk through Camera 001" })).toHaveCount(0);
});
