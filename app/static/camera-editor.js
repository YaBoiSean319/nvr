(function installCameraEditor(root, createModule) {
  const editor = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = editor;
  root.NvrCameraEditor = editor;
})(typeof window === "undefined" ? globalThis : window, function createCameraEditor() {
  const CAMERA_EDITOR_TABS = Object.freeze([
    Object.freeze({ id: "general-connection", label: "General & Connection", sections: Object.freeze(["camera-details", "camera-source", "camera-audio", "camera-credentials"]) }),
    Object.freeze({ id: "recording", label: "Recording", sections: Object.freeze(["camera-recording-policy"]) }),
    Object.freeze({ id: "motion", label: "Motion", sections: Object.freeze(["camera-motion-roll", "camera-motion-source"]) }),
    Object.freeze({ id: "ai", label: "AI", sections: Object.freeze(["camera-ai"]) }),
    Object.freeze({ id: "schedule-retention", label: "Schedule & Retention", sections: Object.freeze(["camera-schedule", "camera-retention"]) }),
    Object.freeze({ id: "health-history", label: "Health & History", sections: Object.freeze([]) }),
  ]);
  const tabIds = new Set(CAMERA_EDITOR_TABS.map(tab => tab.id));

  function createCameraEditorState() {
    let selectedCameraId = null;
    let activeTab = null;
    let generation = 0;

    return {
      get selectedCameraId() { return selectedCameraId; },
      get activeTab() { return activeTab; },
      get generation() { return generation; },
      selectCamera(cameraId) {
        const nextId = cameraId == null ? null : String(cameraId);
        if (nextId === selectedCameraId) return false;
        selectedCameraId = nextId;
        activeTab = nextId ? "general-connection" : null;
        generation += 1;
        return true;
      },
      selectTab(tabId) {
        if (!tabIds.has(tabId) || !selectedCameraId) return false;
        activeTab = tabId;
        return true;
      },
      sectionsForTab(tabId) {
        return CAMERA_EDITOR_TABS.find(tab => tab.id === tabId)?.sections.slice() || [];
      },
      dirtySections(tabId, dirtySectionIds) {
        const dirty = new Set(dirtySectionIds || []);
        return this.sectionsForTab(tabId).filter(sectionId => dirty.has(sectionId));
      },
      allDirtySections(dirtySectionIds) {
        const dirty = new Set(dirtySectionIds || []);
        return CAMERA_EDITOR_TABS.flatMap(tab => tab.sections.filter(sectionId => dirty.has(sectionId)));
      },
      captureSelection() {
        return Object.freeze({ cameraId: selectedCameraId, generation });
      },
      isCurrent(selection) {
        return Boolean(selection && selection.cameraId === selectedCameraId && selection.generation === generation);
      },
    };
  }

  async function resolveDirtyNavigation(decision, { save, discard } = {}) {
    if (decision === "cancel") return false;
    if (decision === "discard") {
      await discard?.();
      return true;
    }
    if (decision === "save") return Boolean(await save?.());
    return false;
  }

  return { CAMERA_EDITOR_TABS, createCameraEditorState, resolveDirtyNavigation };
});
