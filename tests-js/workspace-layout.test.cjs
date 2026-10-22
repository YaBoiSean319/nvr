const test = require("node:test");
const assert = require("node:assert/strict");
const { autoLayoutForCount, reconcileCameraOrder } = require("../app/static/workspace-layout.js");

test("automatic Live layout follows the approved camera-count arrangements", () => {
  const expected = new Map([
    [0, [1, 1, 0]], [1, [1, 1, 1]], [2, [2, 1, 2]], [3, [2, 2, 4]],
    [4, [2, 2, 4]], [5, [3, 2, 6]], [6, [3, 2, 6]], [7, [3, 3, 9]],
    [9, [3, 3, 9]], [10, [4, 3, 12]], [12, [4, 3, 12]], [13, [4, 4, 16]],
    [16, [4, 4, 16]],
  ]);

  for (const [count, [columns, rows, capacity]] of expected) {
    assert.deepEqual(autoLayoutForCount(count, 1280), { columns, rows, capacity });
  }
});

test("automatic layout adapts to narrow viewports without dropping cameras", () => {
  assert.deepEqual(autoLayoutForCount(5, 390), { columns: 1, rows: 5, capacity: 5 });
  assert.deepEqual(autoLayoutForCount(5, 800), { columns: 2, rows: 3, capacity: 6 });
});

test("camera reconciliation preserves existing order and appends additions", () => {
  assert.deepEqual(
    reconcileCameraOrder(["a", "b", "c"], ["c", "a", "d"]),
    ["a", "c", "d"],
  );
});

test("camera reconciliation rejects an over-capacity selection instead of truncating it", () => {
  const cameras = Array.from({ length: 17 }, (_, index) => `camera-${index}`);
  assert.throws(() => reconcileCameraOrder([], cameras), /at most 16/);
  assert.throws(() => autoLayoutForCount(17, 1280), /at most 16/);
});
