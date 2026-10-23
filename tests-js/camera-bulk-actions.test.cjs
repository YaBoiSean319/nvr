const test = require("node:test");
const assert = require("node:assert/strict");
const { selectionCounts, createReview, reconcileResults, retryableIds, plannedState, mountWorkspace } = require("../app/static/camera-bulk-actions.js");

test("bulk selection counts retain hidden and unavailable IDs without changing the selected set", () => {
  const cameras = [{ id: "a" }, { id: "b" }, { id: "archived", archived_at: "today" }];
  assert.deepEqual(selectionCounts(["a", "b", "archived", "gone", "a"], cameras, ["a"]), {
    selected: 4,
    hidden: 2,
    unavailable: 1,
  });
});

test("bulk review captures a stable exact ID/name snapshot and marks stale or archived targets", () => {
  const cameras = [
    { id: "a", name: "Front Door" },
    { id: "b", name: "Lot", archived_at: "today" },
  ];
  const review = createReview(["a", "b", "gone", "a"], cameras, ["a"], { recording_mode: "continuous" });
  cameras[0].name = "Renamed after review";
  assert.deepEqual(review.cameraIds, ["a", "b", "gone"]);
  assert.deepEqual(review.targets, [
    { id: "a", name: "Front Door", status: "ready", current: { groupName: null, recordingMode: null, effectiveRecordingMode: null, recordingEnabled: false } },
    { id: "b", name: "Lot", status: "archived" },
    { id: "gone", name: "Camera no longer available", status: "missing" },
  ]);
  assert.deepEqual(review.changes, { recording_mode: "continuous" });
});

test("uncertain outcomes reconcile from persisted settings and retry only eligible failures", () => {
  const results = reconcileResults(["done", "failed", "unapplied", "archived", "gone"], [
    { camera_id: "done", status: "success" },
    { camera_id: "failed", status: "failed", code: "update_failed" },
    { camera_id: "unapplied", status: "unknown", code: "outcome_unknown" },
    { camera_id: "archived", status: "unknown", code: "outcome_unknown" },
    { camera_id: "gone", status: "unknown", code: "outcome_unknown" },
  ], [
    { id: "done", recording_mode: "continuous", recording_enabled: true },
    { id: "failed", recording_mode: "motion", recording_enabled: true },
    { id: "unapplied", recording_mode: "motion", recording_enabled: false },
    { id: "archived", archived_at: "today", recording_mode: "motion", recording_enabled: false },
  ], { recording_mode: "continuous", recording_enabled: true });

  assert.deepEqual(results.map(result => result.status), ["success", "failed", "failed", "failed", "failed"]);
  assert.deepEqual(results.map(result => result.code), [undefined, "update_failed", "not_applied", "archived", "missing"]);
  assert.deepEqual(retryableIds(results, [
    { id: "done" }, { id: "failed" }, { id: "unapplied" }, { id: "archived", archived_at: "today" },
  ]), ["failed", "unapplied"]);
  assert.deepEqual(retryableIds([
    { camera_id: "no-longer-authorized", status: "failed", code: "unauthorized" },
  ], [{ id: "no-longer-authorized" }]), []);
  assert.equal(reconcileResults(["still-unknown"], [], null, {}).at(0).status, "unknown");
  const disabledMismatch = reconcileResults(["camera"], [{ camera_id: "camera", status: "unknown" }], [
    { id: "camera", recording_mode: "disabled", recording_enabled: true },
  ], { recording_mode: "disabled" });
  assert.equal(disabledMismatch[0].status, "failed");
  assert.equal(disabledMismatch[0].code, "not_applied");
});

test("bulk review keeps a disabled-mode camera disabled when enablement is requested", () => {
  const review = createReview(["disabled"], [
    { id: "disabled", name: "Disabled camera", recording_mode: "disabled", effective_recording_mode: "disabled", recording_enabled: false },
  ], ["disabled"], { recording_enabled: true });
  assert.deepEqual(plannedState(review.targets[0], review.changes), {
    groupName: null,
    recordingMode: "disabled",
    recordingEnabled: false,
    enablementBlockedByMode: true,
  });
  const reconciled = reconcileResults(["disabled"], [{ camera_id: "disabled", status: "unknown" }], [
    { id: "disabled", recording_mode: "disabled", recording_enabled: false },
  ], review.changes);
  assert.equal(reconciled[0].status, "success");
  assert.equal(reconciled[0].code, "reconciled");
});

test("bulk module exposes the workspace controller seam", () => {
  assert.equal(typeof mountWorkspace, "function");
  assert.equal(mountWorkspace(null, {}), null);
});
