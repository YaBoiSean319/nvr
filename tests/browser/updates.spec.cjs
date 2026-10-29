// Guided updates (#148): the Updates page shows a newer release with its notes and migration flag, lists the pre-update backup before it shows the host commands, and the
// rollback names the database copy that exists. The server's own behaviour is covered by tests/test_updates.py; here the API is answered by the journey so the page's flow is exact.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const RELEASE = {
  version: "0.2.0", tag: "v0.2.0", notes: "Two-way audio.\nA new Updates page.", url: "https://github.com/yaboisean319/nvr/releases/tag/v0.2.0", published_at: "2026-10-20T12:00:00Z",
  image: "ghcr.io/yaboisean319/nvr:0.2.0", schema_version: 44, has_migrations: true,
};
const STATE = {
  version: "0.1.0", schema_version: 43, image: "ghcr.io/yaboisean319/nvr:0.1.0", check_enabled: true, disabled_by_environment: false, checking: true,
  last_checked_at: "2026-10-28T10:00:00Z", last_error: null, available: RELEASE, prepared: null, history: [], applied_migrations: [{ version: 43, name: "updates", applied_at: "2026-10-20T10:00:00Z" }],
};
const PLAN = {
  target: RELEASE, backup: { id: "run-1", status: "succeeded", file_name: "nvr-config-20261028T100500Z.nvrbackup", size_bytes: 20480, finished_at: "2026-10-28T10:05:00Z" }, backup_destination: "/backups",
  without_backup: false, upgrade: ["docker compose images nvr", "# In compose.yaml set:   image: ghcr.io/yaboisean319/nvr:0.2.0", "docker compose pull nvr", "docker compose up -d nvr"],
  pre_migration_copies: [{ file: "nvr.sqlite3.pre-migration-v42-to-v43-20261020T100000000000Z", from_schema: 42, to_schema: 43, size_bytes: 4096 }],
  rollback: { steps: ["docker compose stop nvr", "docker compose run --rm --no-deps --entrypoint sh nvr -c 'cp /data/nvr.sqlite3.pre-migration-v42-to-v43-20261020T100000000000Z /data/nvr.sqlite3'", "docker compose up -d nvr"],
    copy: "nvr.sqlite3.pre-migration-v42-to-v43-20261020T100000000000Z", previous_image: "ghcr.io/yaboisean319/nvr:0.1.0", has_copy: true },
  will_migrate: true, note: "These commands are for you to run on the machine that runs the NVR. The NVR cannot run them and has no access to Docker.",
};

test("a newer release is shown, the backup is listed before the commands, and the rollback names the database copy", async ({ page }) => {
  seedFixtures(1);
  let state = STATE;
  let planReady = false;
  const calls = [];
  await page.route("**/api/admin/updates", route => route.fulfill({ json: state }));
  await page.route("**/api/admin/updates/prepare", route => {
    calls.push("prepare");
    state = { ...STATE, prepared: { at: "2026-10-28T10:04:00Z", run_id: "run-1", without_backup: false } };
    route.fulfill({ status: 202, json: { run: { id: "run-1", status: "queued" } } });
  });
  await page.route("**/api/admin/updates/plan", route => (planReady ? route.fulfill({ json: PLAN }) : route.fulfill({ status: 409, json: { detail: "The pre-update backup is still running" } })));
  await signInNext(page);
  await page.goto("/settings/updates");

  await expect(page.getByText("Running version 0.1.0")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Version 0.2.0 is available" })).toBeVisible();
  await expect(page.getByLabel("Release notes for 0.2.0")).toContainText("A new Updates page.");
  await expect(page.getByText(/It changes the database \(schema v43 to v44\)/)).toBeVisible();
  await expect(page.getByRole("region", { name: "Upgrade steps" })).toHaveCount(0); // No commands before the backup

  await page.getByRole("button", { name: "Back up, then show the upgrade steps" }).click();
  const steps = page.getByRole("region", { name: "Upgrade steps" });
  await expect(steps.getByText("The pre-update backup is still running")).toBeVisible();
  await expect(steps.getByLabel("Upgrade commands")).toHaveCount(0);

  planReady = true;
  await expect(steps.getByText("nvr-config-20261028T100500Z.nvrbackup")).toBeVisible({ timeout: 15_000 });
  await expect(steps.getByLabel("Upgrade commands")).toContainText("image: ghcr.io/yaboisean319/nvr:0.2.0");
  await expect(steps.getByLabel("Upgrade commands")).toContainText("docker compose up -d nvr");
  // The backup is above the commands on the page.
  const backupBox = await steps.getByText("nvr-config-20261028T100500Z.nvrbackup").boundingBox();
  const commandsBox = await steps.getByLabel("Upgrade commands").boundingBox();
  expect(backupBox.y).toBeLessThan(commandsBox.y);
  await expect(steps.getByText("nvr.sqlite3.pre-migration-v42-to-v43-20261020T100000000000Z").first()).toBeVisible();
  await expect(steps.getByLabel("Rollback commands")).toContainText("pre-migration-v42-to-v43");
  await expect(steps.getByText(/cannot run them and has no access to Docker/)).toBeVisible();
  expect(calls).toEqual(["prepare"]);
});

test("with the check off nothing can be checked, and a page with no newer release says so", async ({ page }) => {
  seedFixtures(1);
  await page.route("**/api/admin/updates", route => route.fulfill({ json: { ...STATE, check_enabled: false, checking: false, available: null, last_checked_at: null } }));
  let checked = 0;
  await page.route("**/api/admin/updates/check", route => { checked += 1; route.fulfill({ status: 409, json: { detail: "The update check is turned off" } }); });
  await signInNext(page);
  await page.goto("/settings/updates");
  await expect(page.getByRole("button", { name: "Check now" })).toBeDisabled();
  await expect(page.getByLabel(/Check for new releases/)).not.toBeChecked();
  expect(checked).toBe(0);
  await expect(page.getByRole("region", { name: "Available release" })).toHaveCount(0);
});
