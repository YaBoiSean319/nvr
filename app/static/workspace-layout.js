(function exposeWorkspaceLayout(root) {
  "use strict";

  function autoLayoutForCount(cameraCount, viewportWidth = 1280) {
    if (!Number.isInteger(cameraCount) || cameraCount < 0 || cameraCount > 16) {
      throw new RangeError("Automatic layouts support at most 16 cameras.");
    }
    if (cameraCount === 0) return { columns: 1, rows: 1, capacity: 0 };
    if (viewportWidth <= 600) return { columns: 1, rows: cameraCount, capacity: cameraCount };
    if (viewportWidth <= 960) {
      const columns = Math.min(2, cameraCount);
      return { columns, rows: Math.ceil(cameraCount / columns), capacity: columns * Math.ceil(cameraCount / columns) };
    }

    if (cameraCount === 1) return { columns: 1, rows: 1, capacity: 1 };
    if (cameraCount === 2) return { columns: 2, rows: 1, capacity: 2 };
    if (cameraCount <= 4) return { columns: 2, rows: 2, capacity: 4 };
    if (cameraCount <= 6) return { columns: 3, rows: 2, capacity: 6 };
    if (cameraCount <= 9) return { columns: 3, rows: 3, capacity: 9 };
    if (cameraCount <= 12) return { columns: 4, rows: 3, capacity: 12 };
    return { columns: 4, rows: 4, capacity: 16 };
  }

  function reconcileCameraOrder(currentIds, selectedIds, maxCameras = 16) {
    const selected = new Set(selectedIds);
    const ordered = [...new Set(currentIds)].filter(id => selected.has(id));
    const included = new Set(ordered);
    for (const id of selectedIds) {
      if (!included.has(id)) {
        included.add(id);
        ordered.push(id);
      }
    }
    if (ordered.length > maxCameras) throw new RangeError(`Choose at most ${maxCameras} cameras.`);
    return ordered;
  }

  const api = Object.freeze({ autoLayoutForCount, reconcileCameraOrder });
  root.NvrWorkspaceLayout = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis === "undefined" ? window : globalThis);
