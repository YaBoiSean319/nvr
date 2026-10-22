(function installCameraBulkActions(root, createModule) {
  const bulkActions = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = bulkActions;
  root.NvrCameraBulkActions = bulkActions;
})(typeof window === "undefined" ? globalThis : window, function createCameraBulkActions() {
  function uniqueIds(values) {
    return [...new Set([...values].map(value => String(value)))];
  }

  function selectionCounts(selectedIds, cameras, visibleIds) {
    const selected = new Set(uniqueIds(selectedIds));
    const cameraIds = new Set((cameras || []).map(camera => String(camera.id)));
    const visible = new Set((visibleIds || []).map(value => String(value)));
    let hidden = 0;
    let unavailable = 0;
    for (const id of selected) {
      if (!cameraIds.has(id)) unavailable += 1;
      else if (!visible.has(id)) hidden += 1;
    }
    return { selected: selected.size, hidden, unavailable };
  }

  function createReview(selectedIds, cameras, visibleIds, changes) {
    const cameraById = new Map((cameras || []).map(camera => [String(camera.id), camera]));
    const cameraIds = uniqueIds(selectedIds);
    const targets = cameraIds.map(id => {
      const camera = cameraById.get(id);
      if (!camera) return { id, name: "Camera no longer available", status: "missing" };
      if (camera.archived_at) return { id, name: camera.name || id, status: "archived" };
      return {
        id,
        name: camera.name || id,
        status: "ready",
        current: {
          groupName: camera.group_name || null,
          recordingMode: camera.recording_mode || null,
          effectiveRecordingMode: camera.effective_recording_mode || camera.recording_mode || null,
          recordingEnabled: Boolean(camera.recording_enabled),
        },
      };
    });
    return {
      cameraIds,
      targets,
      changes: { ...changes },
      counts: selectionCounts(cameraIds, cameras, visibleIds),
    };
  }

  function orderedResults(targetIds, results) {
    const byId = new Map((results || []).map(result => [String(result.camera_id), result]));
    return uniqueIds(targetIds).map(id => byId.get(id) || {
      camera_id: id,
      status: "unknown",
      code: "missing_result",
      detail: "The server did not report an outcome for this camera.",
    });
  }

  function changesMatch(camera, changes) {
    if (Object.prototype.hasOwnProperty.call(changes, "group_name") && (camera.group_name || null) !== (changes.group_name || null)) return false;
    if (changes.recording_mode && camera.recording_mode !== changes.recording_mode) return false;
    if (Object.prototype.hasOwnProperty.call(changes, "recording_enabled")) {
      const resultingMode = changes.recording_mode || camera.recording_mode;
      const expected = Boolean(changes.recording_enabled && resultingMode !== "disabled");
      if (Boolean(camera.recording_enabled) !== expected) return false;
    }
    if (changes.recording_mode === "disabled" && camera.recording_enabled) return false;
    return true;
  }

  function plannedState(target, changes = {}) {
    const current = target.current || {};
    const mode = changes.recording_mode || current.effectiveRecordingMode || current.recordingMode || "Not reported";
    const resultingMode = changes.recording_mode || current.recordingMode;
    let recordingEnabled = Boolean(current.recordingEnabled);
    if (Object.prototype.hasOwnProperty.call(changes, "recording_enabled")) {
      recordingEnabled = Boolean(changes.recording_enabled && resultingMode !== "disabled");
    }
    if (resultingMode === "disabled") recordingEnabled = false;
    return {
      groupName: Object.prototype.hasOwnProperty.call(changes, "group_name") ? changes.group_name || null : current.groupName || null,
      recordingMode: mode,
      recordingEnabled,
      enablementBlockedByMode: changes.recording_enabled === true && resultingMode === "disabled",
    };
  }

  function reconcileResults(targetIds, results, cameras, changes) {
    if (!Array.isArray(cameras)) return orderedResults(targetIds, results);
    const cameraById = new Map((cameras || []).map(camera => [String(camera.id), camera]));
    return orderedResults(targetIds, results).map(result => {
      if (result.status !== "unknown" && result.status !== "failed") return { ...result };
      const camera = cameraById.get(String(result.camera_id));
      if (!camera) return { ...result, status: "failed", code: "missing", detail: "Camera is no longer available; it was not retried." };
      if (camera.archived_at) return { ...result, status: "failed", code: "archived", detail: "Camera is archived; it was not retried." };
      if (changesMatch(camera, changes)) return { ...result, status: "success", code: "reconciled", detail: "The requested settings are visible in the inventory." };
      if (result.status === "failed" && !["missing", "archived", "unavailable", "not_applied"].includes(result.code)) return { ...result };
      return { ...result, status: "failed", code: "not_applied", detail: "The requested settings are not visible in the inventory and can be retried." };
    });
  }

  function retryableIds(results, cameras) {
    const activeIds = new Set((cameras || []).filter(camera => !camera.archived_at).map(camera => String(camera.id)));
    return (results || [])
      .filter(result => result.status === "failed" && result.code !== "unauthorized" && result.code !== "permission_denied" && activeIds.has(String(result.camera_id)))
      .map(result => String(result.camera_id));
  }

  return { selectionCounts, createReview, orderedResults, reconcileResults, retryableIds, plannedState };
});
