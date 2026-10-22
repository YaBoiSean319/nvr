(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.NvrCameraGroups = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  const GROUP_POLICY_TABS = Object.freeze([
    Object.freeze({ id: "general", label: "Group & Members" }),
    Object.freeze({ id: "recording", label: "Schedule & Retention" }),
    Object.freeze({ id: "storage", label: "Storage" }),
    Object.freeze({ id: "ai", label: "AI" }),
  ]);

  function createGroupWorkspaceState() {
    let groups = [];
    let selectedGroupId = null;
    let activeTab = GROUP_POLICY_TABS[0].id;
    let query = "";
    let generation = 0;

    return {
      get groups() { return groups.slice(); },
      get selectedGroupId() { return selectedGroupId; },
      get activeTab() { return activeTab; },
      get query() { return query; },
      get generation() { return generation; },
      setGroups(nextGroups) {
        groups = Array.isArray(nextGroups) ? nextGroups.slice() : [];
        if (selectedGroupId && !groups.some((group) => group.id === selectedGroupId)) {
          selectedGroupId = null;
          generation += 1;
          activeTab = GROUP_POLICY_TABS[0].id;
        }
        return this.groups;
      },
      setQuery(value) {
        query = String(value || "").trim().toLocaleLowerCase();
        return this.visibleGroups();
      },
      visibleGroups() {
        if (!query) return groups.slice();
        return groups.filter((group) => String(group.name || "").toLocaleLowerCase().includes(query));
      },
      selectGroup(groupId) {
        if (!groups.some((group) => group.id === groupId)) return false;
        if (selectedGroupId !== groupId) {
          generation += 1;
          activeTab = GROUP_POLICY_TABS[0].id;
        }
        selectedGroupId = groupId;
        return true;
      },
      clearSelection() {
        if (selectedGroupId !== null) generation += 1;
        selectedGroupId = null;
        activeTab = GROUP_POLICY_TABS[0].id;
      },
      selectedGroup() {
        return groups.find((group) => group.id === selectedGroupId) || null;
      },
      selectTab(tabId) {
        if (!GROUP_POLICY_TABS.some((tab) => tab.id === tabId)) return false;
        activeTab = tabId;
        return true;
      },
      captureSelection() {
        return Object.freeze({ groupId: selectedGroupId, generation });
      },
      isCurrent(selection) {
        return Boolean(selection && selection.groupId === selectedGroupId && selection.generation === generation);
      },
    };
  }

  return { GROUP_POLICY_TABS, createGroupWorkspaceState };
});
