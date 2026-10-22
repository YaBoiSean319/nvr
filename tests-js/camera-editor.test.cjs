const test = require("node:test");
const assert = require("node:assert/strict");
const { createCameraEditorState, CAMERA_EDITOR_TABS, resolveDirtyNavigation } = require("../app/static/camera-editor.js");

test("a new camera editor has no selected camera or active settings tab", () => {
  const editor = createCameraEditorState();

  assert.equal(editor.selectedCameraId, null);
  assert.equal(editor.activeTab, null);
});

test("selecting a camera opens General and changing cameras never carries the prior tab", () => {
  const editor = createCameraEditorState();

  assert.equal(editor.selectCamera("camera-one"), true);
  assert.equal(editor.activeTab, "general-connection");
  assert.equal(editor.selectTab("motion"), true);
  assert.equal(editor.selectCamera("camera-one"), false);
  assert.equal(editor.activeTab, "motion");
  assert.equal(editor.selectCamera("camera-two"), true);
  assert.equal(editor.activeTab, "general-connection");
});

test("camera setting tabs map the existing configuration sections without overlap", () => {
  const editor = createCameraEditorState();

  assert.deepEqual(editor.sectionsForTab("general-connection"), [
    "camera-details", "camera-source", "camera-audio", "camera-credentials",
  ]);
  assert.deepEqual(editor.sectionsForTab("recording"), ["camera-recording-policy"]);
  assert.deepEqual(editor.sectionsForTab("motion"), ["camera-motion-roll", "camera-motion-source"]);
  assert.deepEqual(editor.sectionsForTab("ai"), ["camera-ai"]);
  assert.deepEqual(editor.sectionsForTab("schedule-retention"), ["camera-schedule", "camera-retention"]);
  assert.deepEqual(editor.sectionsForTab("health-history"), []);
  assert.equal(new Set(CAMERA_EDITOR_TABS.map(tab => tab.id)).size, 6);
});

test("tab saves include only dirty sections in the current tab", () => {
  const editor = createCameraEditorState();
  const dirty = ["camera-source", "camera-motion-roll", "camera-retention"];

  assert.deepEqual(editor.dirtySections("general-connection", dirty), ["camera-source"]);
  assert.deepEqual(editor.dirtySections("schedule-retention", dirty), ["camera-retention"]);
  assert.deepEqual(editor.dirtySections("ai", dirty), []);
});

test("unknown tabs cannot replace the current editor section", () => {
  const editor = createCameraEditorState();
  editor.selectCamera("camera-one");

  assert.equal(editor.selectTab("unsupported"), false);
  assert.equal(editor.activeTab, "general-connection");
});

test("async responses are current only for the camera selection that started them", () => {
  const editor = createCameraEditorState();
  editor.selectCamera("camera-one");
  const firstSelection = editor.captureSelection();

  assert.equal(editor.isCurrent(firstSelection), true);
  editor.selectCamera("camera-two");
  assert.equal(editor.isCurrent(firstSelection), false);
  assert.equal(editor.isCurrent(editor.captureSelection()), true);
});

test("all dirty sections retain the editor's documented tab order", () => {
  const editor = createCameraEditorState();

  assert.deepEqual(editor.allDirtySections(["camera-ai", "camera-source", "camera-retention"]), [
    "camera-source", "camera-ai", "camera-retention",
  ]);
});

test("dirty navigation cancels without touching the draft", async () => {
  let saved = false;
  let discarded = false;

  assert.equal(await resolveDirtyNavigation("cancel", {
    save: async () => { saved = true; return true; },
    discard: async () => { discarded = true; },
  }), false);
  assert.equal(saved, false);
  assert.equal(discarded, false);
});

test("dirty navigation proceeds only after a successful save and preserves drafts on failure", async () => {
  assert.equal(await resolveDirtyNavigation("save", { save: async () => false }), false);
  assert.equal(await resolveDirtyNavigation("save", { save: async () => true }), true);
});

test("discard navigation clears drafts only after an explicit discard decision", async () => {
  let discarded = 0;

  assert.equal(await resolveDirtyNavigation("discard", { discard: async () => { discarded += 1; } }), true);
  assert.equal(discarded, 1);
});
