(function installCameraInventory(root, createModule) {
  const inventory = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = inventory;
  root.NvrCameraInventory = inventory;
})(typeof window === "undefined" ? globalThis : window, function createCameraInventory() {
  function normalizedState(value) {
    return typeof value === "string" && value.trim() ? value.trim().toLocaleLowerCase() : "unknown";
  }

  function valueFor(camera, key) {
    if (key === "name") return camera.name || "";
    if (key === "ip") return camera.camera_ip || "";
    if (key === "group") return camera.group_name || "";
    if (key === "connection") return normalizedState(camera.connection_status);
    if (key === "health") return normalizedState(camera.recording_health?.state);
    if (key === "mode") return camera.effective_recording_mode || camera.recording_mode || "unknown";
    return "";
  }

  function filterAndSortCameras(cameras, filters = {}) {
    const query = (filters.query || "").trim().toLocaleLowerCase();
    const archive = filters.archive || "active";
    const group = filters.group || "all";
    const connection = filters.connection || "all";
    const health = filters.health || "all";
    const sortKey = filters.sortKey || "name";
    const sortDirection = filters.sortDirection === -1 ? -1 : 1;
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

    return cameras.filter(camera => {
      if (archive === "active" && camera.archived_at) return false;
      if (archive === "archived" && !camera.archived_at) return false;
      if (query && !`${camera.name || ""} ${camera.camera_ip || ""}`.toLocaleLowerCase().includes(query)) return false;
      if (group === "__none__" && camera.group_name) return false;
      if (group !== "all" && group !== "__none__" && (camera.group_name || "") !== group) return false;
      if (connection !== "all" && normalizedState(camera.connection_status) !== connection) return false;
      if (health !== "all" && normalizedState(camera.recording_health?.state) !== health) return false;
      return true;
    }).sort((left, right) => {
      const compared = collator.compare(String(valueFor(left, sortKey)), String(valueFor(right, sortKey)));
      return compared === 0 ? collator.compare(String(left.id), String(right.id)) : compared * sortDirection;
    });
  }

  function selectedCamera(cameras, selectedCameraId) {
    return selectedCameraId ? cameras.find(camera => camera.id === selectedCameraId) || null : null;
  }

  function stateLabel(value) {
    const state = normalizedState(value);
    if (state === "unknown") return "Not reported";
    return state.replaceAll("_", " ").replace(/\b\w/g, letter => letter.toLocaleUpperCase());
  }

  return { filterAndSortCameras, selectedCamera, stateLabel };
});
