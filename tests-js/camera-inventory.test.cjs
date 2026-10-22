const test = require("node:test");
const assert = require("node:assert/strict");
const { filterAndSortCameras, selectedCamera } = require("../app/static/camera-inventory.js");

test("an empty camera inventory remains empty instead of selecting a placeholder", () => {
  assert.deepEqual(filterAndSortCameras([], {}), []);
  assert.equal(selectedCamera([], null), null);
});

test("a non-empty inventory opens no editor until a camera is explicitly selected", () => {
  const cameras = [{ id: "one", name: "Front Door" }, { id: "two", name: "Loading Bay" }];

  assert.equal(selectedCamera(cameras, null), null);
  assert.equal(selectedCamera(cameras, "two").id, "two");
  assert.equal(selectedCamera(cameras, "deleted"), null);
});

test("the inventory is sorted by name with deterministic ties for a 200-camera installation", () => {
  const cameras = Array.from({ length: 200 }, (_, index) => ({
    id: `camera-${String(199 - index).padStart(3, "0")}`,
    name: `Camera ${String(199 - index).padStart(3, "0")}`,
    camera_ip: `192.0.2.${index + 1}`,
    recording_health: { state: "recording" },
  }));

  const ordered = filterAndSortCameras(cameras, {});

  assert.equal(ordered.length, 200);
  assert.equal(ordered[0].name, "Camera 000");
  assert.equal(ordered[199].name, "Camera 199");
  assert.deepEqual(filterAndSortCameras([
    { id: "b", name: "Front Door" },
    { id: "a", name: "Front Door" },
  ], {}).map(item => item.id), ["a", "b"]);
});

test("inventory search matches names and addresses, while filters distinguish archives and health", () => {
  const cameras = [
    { id: "a", name: "West Door", camera_ip: "192.0.2.14", group_name: "Loading" },
    { id: "b", name: "East Door", camera_ip: "192.0.2.15", group_name: "Loading", archived_at: "2026-01-01T00:00:00Z" },
    { id: "c", name: "Back Gate", camera_ip: "192.0.2.16", group_name: "Loading", recording_health: { state: "uncertain" } },
  ];

  assert.deepEqual(filterAndSortCameras(cameras, { query: "192.0.2.15", archive: "all" }).map(item => item.id), ["b"]);
  assert.deepEqual(filterAndSortCameras(cameras, { query: "Loading" }).map(item => item.id), []);
  assert.deepEqual(filterAndSortCameras(cameras, { archive: "active" }).map(item => item.id), ["c", "a"]);
  assert.deepEqual(filterAndSortCameras(cameras, { archive: "archived" }).map(item => item.id), ["b"]);
  assert.deepEqual(filterAndSortCameras(cameras, { group: "Loading" }).map(item => item.id), ["c", "a"]);
  assert.deepEqual(filterAndSortCameras(cameras, { health: "uncertain" }).map(item => item.id), ["c"]);
  assert.deepEqual(filterAndSortCameras(cameras, { group: "__none__" }).map(item => item.id), []);
});

test("unknown connection and recording health values remain explicitly filterable", () => {
  const cameras = [{ id: "a", name: "No report", recording_status: "recording" }];

  assert.equal(filterAndSortCameras(cameras, { connection: "unknown" }).length, 1);
  assert.equal(filterAndSortCameras(cameras, { health: "unknown" }).length, 1);
  assert.equal(filterAndSortCameras(cameras, { connection: "recording" }).length, 0);
});
