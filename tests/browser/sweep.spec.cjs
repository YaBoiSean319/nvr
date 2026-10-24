// Responsive and accessibility sweep across the whole interface (#104, #77): every workspace
// at phone, tablet and desktop widths, in each theme, with reduced motion, for both roles.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const WIDTHS = [320, 360, 768, 1024, 1440].map(width => ({ width, height: 800 }));
// The desktop sizes #77 names for operators' monitoring stations.
const DESKTOPS = [{ width: 1920, height: 1080 }, { width: 1440, height: 900 }, { width: 1366, height: 768 }];
const SETTINGS = ["general", "appearance", "access", "recording", "notifications", "integrations", "diagnostics"];
const ADMIN_ROUTES = ["/live", "/playback", "/cameras", "/cameras/add", "/cameras/groups", "/cameras/rules", ...SETTINGS.map(category => `/settings/${category}`)];
const OPERATOR_ROUTES = ["/live", "/playback", "/cameras"];
// Roles that are operated by name; any of these without one is unusable with a screen reader.
const NAMED_ROLES = ["button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "slider", "spinbutton", "switch", "tab", "menuitem", "separator"];

async function setTheme(page, theme) {
  const response = await page.request.put("/api/me/preferences", { data: { theme } });
  expect(response.ok()).toBeTruthy();
}

/** Open a route and wait until the workspace has rendered rather than the loading shell. */
async function visit(page, route) {
  await page.goto(route);
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
  expect(new URL(page.url()).pathname, "the route exists rather than redirecting elsewhere").toBe(route);
  await expect(page.locator("main")).not.toHaveText(/^\s*$/);
  await page.waitForLoadState("networkidle").catch(() => undefined);
}

async function expectFits(page, label) {
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - window.innerWidth,
    body: document.body.scrollWidth - window.innerWidth,
  }));
  expect(overflow.document, `${label}: the page scrolls sideways`).toBeLessThanOrEqual(0);
  expect(overflow.body, `${label}: the page scrolls sideways`).toBeLessThanOrEqual(0);
}

async function expectNamedControls(page, label) {
  const snapshot = await page.locator("body").ariaSnapshot();
  const unnamed = snapshot.split("\n")
    .map(line => line.trim())
    .filter(line => NAMED_ROLES.some(role => new RegExp(`^- ${role}(?:$|:| \\[)`).test(line)));
  expect(unnamed, `${label}: controls without an accessible name`).toEqual([]);
}

async function sweep(page, routes, label) {
  for (const route of routes) {
    await visit(page, route);
    await expectFits(page, `${label} ${route}`);
    await expectNamedControls(page, `${label} ${route}`);
  }
}

for (const size of [...WIDTHS, ...DESKTOPS]) {
  test(`administrator workspaces fit and name their controls at ${size.width}×${size.height}`, async ({ page }) => {
    test.setTimeout(120_000);
    seedFixtures(3, { footage: true });
    await page.setViewportSize(size);
    await signInNext(page);
    await setTheme(page, "light");
    await sweep(page, ADMIN_ROUTES, `${size.width}px`);
  });
}

for (const size of [{ width: 320, height: 640 }, { width: 1366, height: 768 }]) {
  test(`operator workspaces fit and name their controls at ${size.width}px`, async ({ page }) => {
    seedFixtures(3, { footage: true });
    await page.setViewportSize(size);
    await signInNext(page, "camera-operator");
    await sweep(page, OPERATOR_ROUTES, `operator ${size.width}px`);
    await page.goto("/settings/general");
    await expect(page.getByRole("heading", { name: "Administrator access required" })).toBeVisible();
  });
}

const THEMES = [
  { name: "light", preference: "light", scheme: "light", expected: "light" },
  { name: "dark", preference: "dark", scheme: "light", expected: "dark" },
  { name: "system (dark device)", preference: "system", scheme: "dark", expected: null },
  { name: "branded", preference: "light", scheme: "light", expected: "light", branding: { app_name: "Sweep Fixture NVR", primary_color: "#0f3d2e", accent_color: "#f6f064" } },
];

for (const theme of THEMES) {
  test(`the ${theme.name} theme keeps text legible on every workspace`, async ({ page }) => {
    test.setTimeout(150_000);
    seedFixtures(2, { footage: true });
    await page.emulateMedia({ colorScheme: theme.scheme });
    await signInNext(page);
    if (theme.branding) expect((await page.request.put("/api/settings/branding", { data: theme.branding })).ok()).toBeTruthy();
    await setTheme(page, theme.preference);
    for (const size of [{ width: 360, height: 800 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(size);
      for (const route of ADMIN_ROUTES) {
        await visit(page, route);
        if (theme.expected) await expect(page.locator("html")).toHaveAttribute("data-theme", theme.expected);
        else await expect(page.locator("html")).not.toHaveAttribute("data-theme", /light|dark/);
        if (theme.branding) await expect(page.locator(".shell-app-name")).toHaveText(theme.branding.app_name);
        await expectFits(page, `${theme.name} ${size.width}px ${route}`);
        // Body text and accent-coloured text both reach WCAG AA against the page.
        const ratios = await page.evaluate(() => {
          const rgb = value => value.match(/[\d.]+/g).slice(0, 3).map(Number);
          const luminance = ([r, g, b]) => [r, g, b]
            .map(value => { const v = value / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
            .reduce((total, v, index) => total + v * [0.2126, 0.7152, 0.0722][index], 0);
          const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
          const probe = color => {
            const span = document.createElement("span");
            span.style.color = color;
            document.body.append(span);
            const value = rgb(getComputedStyle(span).color);
            span.remove();
            return value;
          };
          const background = rgb(getComputedStyle(document.body).backgroundColor);
          return { text: contrast(probe("var(--n-text)"), background), accent: contrast(probe("var(--n-accent)"), background), muted: contrast(probe("var(--n-muted)"), background) };
        });
        expect(ratios.text, `${route} body text`).toBeGreaterThanOrEqual(4.5);
        expect(ratios.accent, `${route} accent text`).toBeGreaterThanOrEqual(4.5);
        expect(ratios.muted, `${route} secondary text`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
}

test("reduced motion turns off animation and transitions everywhere", async ({ page }) => {
  seedFixtures(2, { footage: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await signInNext(page);
  for (const route of ADMIN_ROUTES) {
    await visit(page, route);
    const moving = await page.evaluate(() => [...document.querySelectorAll("*")]
      .map(element => {
        const style = getComputedStyle(element);
        const longest = value => Math.max(...value.split(",").map(part => parseFloat(part) * (part.trim().endsWith("ms") ? 1 : 1000)));
        return { element: `${element.tagName.toLowerCase()}.${[...element.classList].join(".")}`, animation: longest(style.animationDuration), transition: longest(style.transitionDuration) };
      })
      .filter(entry => entry.animation > 1 || entry.transition > 1)
      .map(entry => entry.element));
    expect(moving, `${route}: elements still animate`).toEqual([]);
  }
  // Opening a dialog and the status drawer also happens without motion.
  await visit(page, "/live");
  await page.getByRole("button", { name: /^Recording:/ }).click();
  const drawer = page.getByRole("dialog", { name: "Operational status" });
  await expect(drawer).toBeVisible();
  const drawerMotion = await drawer.evaluate(element => [getComputedStyle(element).animationDuration, getComputedStyle(element).transitionDuration]);
  for (const value of drawerMotion) expect(parseFloat(value)).toBeLessThanOrEqual(0.01);
});
