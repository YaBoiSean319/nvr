const test = require("node:test");
const assert = require("node:assert/strict");
const { createCameraEditorState, CAMERA_EDITOR_TABS, resolveDirtyNavigation, createCameraEditorView } = require("../app/static/camera-editor.js");
const { runScopedSave } = require("../app/static/camera-admin-save.js");

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

test("scoped saves are sequential, clear only committed drafts, and always unlock after failure", async () => {
  const events = [];
  const first = { dirty: true };
  const failed = { dirty: true };
  const untouched = { dirty: true };
  const result = await runScopedSave([
    { scope: first, label: "Recording", save: async () => events.push("save:recording"), clear: scope => { scope.dirty = false; } },
    { scope: failed, label: "Motion", save: async () => { events.push("save:motion"); throw new Error("write failed"); }, clear: scope => { scope.dirty = false; } },
    { scope: untouched, label: "AI", save: async () => events.push("save:ai"), clear: scope => { scope.dirty = false; } },
  ], {
    lock: () => { events.push("lock"); return () => events.push("unlock"); },
    onStart: () => events.push("start"),
    onFinish: result => events.push(result.error ? "finish:error" : "finish:success"),
  });

  assert.deepEqual(events, ["lock", "start", "save:recording", "save:motion", "unlock", "finish:error"]);
  assert.deepEqual(result.savedLabels, ["Recording"]);
  assert.equal(result.failedOperation.scope, failed);
  assert.match(result.error.message, /write failed/);
  assert.equal(first.dirty, false);
  assert.equal(failed.dirty, true);
  assert.equal(untouched.dirty, true);
});

test("selected-camera view renders the selected camera through its application service interface", () => {
  const originalDocument = global.document;
  const elements = [];
  const historyVisibility = [];
  class Element {
    constructor(tagName) {
      this.tagName = tagName;
      this.children = [];
      this.dataset = {};
      this.attributes = {};
      this.listeners = {};
      this.classList = { toggle: () => {}, remove: name => historyVisibility.push(["remove", name]) };
    }
    append(...children) { this.children.push(...children); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  global.document = {
    createElement(tagName) { const element = new Element(tagName); elements.push(element); return element; },
    querySelector(selector) { return selector === "#camera-health-history" ? { classList: { remove: name => historyVisibility.push(["remove", name]) } } : null; },
  };
  try {
    const camera = {
      id: "north-door", name: "North door", recording_mode: "continuous", effective_recording_mode: "continuous",
      recording_enabled: true, recording_status: "recording", retention_days: 30,
      retention_coverage: { achieved_days: 12 }, video: { codec: "h264", width: 1280, height: 720 },
      recording_health: { state: "healthy" }, audio_status: "unavailable",
    };
    const loaded = [];
    const mounted = [];
    const view = createCameraEditorView({
      formatDateTime: value => String(value),
      media: {
        loadSegments: value => loaded.push(value.id),
        cameraPreviewWorkspace: { mount: (slot, value) => mounted.push([slot, value.id]) },
      },
    });
    const card = view.render(camera, { role: "operator", selectedCameraId: camera.id, editorState: {} });

    assert.equal(card.tagName, "article");
    assert.equal(card.dataset.cameraId, "north-door");
    assert.equal(card.children[0].textContent, "North door");
    assert.match(card.children[1].textContent, /recording enabled/);
    assert.match(card.children[1].textContent, /h264 1280×720/);
    const previewSlot = card.children.find(child => child.dataset.cameraPreviewSlot === "");
    assert.ok(previewSlot);
    assert.deepEqual(mounted, [[previewSlot, "north-door"]]);
    const historyButton = card.children.find(child => child.tagName === "button");
    assert.equal(historyButton.textContent, "Find recorded footage");
    historyButton.listeners.click();
    assert.deepEqual(loaded, ["north-door"]);
    assert.deepEqual(historyVisibility, [["remove", "hidden"]]);
  } finally {
    if (originalDocument === undefined) delete global.document;
    else global.document = originalDocument;
  }
});
