const test = require("node:test");
const assert = require("node:assert/strict");
const { createGroupWorkspaceState, GROUP_POLICY_TABS } = require("../app/static/camera-groups.js");

test("group workspace starts unselected, filters the list, and preserves selection across refreshes", () => {
  const state = createGroupWorkspaceState();
  state.setGroups([
    { id: "east", name: "East wing" },
    { id: "north", name: "North wing" },
  ]);

  assert.equal(state.selectedGroupId, null);
  assert.deepEqual(state.visibleGroups().map((group) => group.id), ["east", "north"]);
  assert.equal(state.selectGroup("north"), true);
  state.setQuery("east");
  assert.deepEqual(state.visibleGroups().map((group) => group.id), ["east"]);
  assert.equal(state.selectedGroupId, "north");

  state.setGroups([
    { id: "north", name: "North wing renamed" },
    { id: "east", name: "East wing" },
  ]);
  assert.equal(state.selectedGroupId, "north");
  assert.equal(state.selectedGroup().name, "North wing renamed");
  assert.deepEqual(state.visibleGroups().map((group) => group.id), ["east"]);

  const removedSelection = state.captureSelection();
  state.setGroups([{ id: "east", name: "East wing" }]);
  assert.equal(state.selectedGroupId, null);
  assert.equal(state.selectedGroup(), null);
  assert.equal(state.activeTab, "general");
  assert.equal(state.isCurrent(removedSelection), false);
  state.setGroups([{ id: "north", name: "North wing restored" }]);
  assert.equal(state.isCurrent(removedSelection), false);
});

test("group selection and policy tabs reject missing values without changing the active editor", () => {
  const state = createGroupWorkspaceState();
  state.setGroups([{ id: "east", name: "East wing" }]);
  assert.equal(state.selectGroup("missing"), false);
  assert.equal(state.selectedGroupId, null);
  assert.equal(state.selectGroup("east"), true);
  const request = state.captureSelection();
  assert.equal(state.isCurrent(request), true);
  state.setGroups([{ id: "east", name: "East wing" }, { id: "north", name: "North wing" }]);
  assert.equal(state.isCurrent(request), true);
  state.selectGroup("north");
  assert.equal(state.isCurrent(request), false);

  assert.equal(state.selectTab("storage"), true);
  assert.equal(state.activeTab, "storage");
  assert.equal(state.selectTab("made-up"), false);
  assert.equal(state.activeTab, "storage");
  assert.deepEqual(GROUP_POLICY_TABS.map((tab) => tab.id), ["general", "recording", "storage", "ai"]);
  state.selectGroup("east");
  assert.equal(state.activeTab, "general");
  const beforeLogout = state.captureSelection();
  state.clearSelection();
  assert.equal(state.selectedGroupId, null);
  assert.equal(state.activeTab, "general");
  assert.equal(state.isCurrent(beforeLogout), false);
});
