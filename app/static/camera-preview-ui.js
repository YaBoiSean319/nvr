(function installCameraPreviewWorkspace(root, createModule) {
  const workspace = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = workspace;
  root.NvrCameraPreviewWorkspace = workspace;
})(typeof window === "undefined" ? globalThis : window, function createCameraPreviewWorkspaceModule() {
  const labels = {
    idle: "Preview stopped. Select Show Preview to start one live camera stream.",
    loading: "Loading live preview…",
    fresh: "Fresh live preview. Live video does not confirm recording writes.",
    reconnecting: "Reconnecting to live preview…",
    stale: "The last preview frame is stale.",
    lost: "Preview connection lost.",
    unsupported: "Preview is not supported by this server or browser.",
    failed: "Preview could not start.",
    unavailable: "This camera is unavailable for live preview.",
  };
  let panelSequence = 0;

  function createCameraPreviewWorkspace({ controller } = {}) {
    let view = null;
    let previewController = controller;

    function stateText(snapshot) {
      const base = labels[snapshot.state] || "Preview status unavailable.";
      return snapshot.message ? `${base} ${snapshot.message}` : base;
    }

    function renderState(snapshot) {
      if (!view || snapshot.cameraId !== view.cameraId) return;
      view.status.textContent = stateText(snapshot);
      view.status.dataset.state = snapshot.state;
      const retryable = ["failed", "unsupported", "lost"].includes(snapshot.state);
      view.start.hidden = snapshot.state !== "idle";
      view.retry.hidden = !retryable;
      view.close.hidden = snapshot.state === "idle";
    }

    function renderFrame(frame) {
      if (!view || frame.cameraId !== view.cameraId) return;
      if (frame.url) {
        view.image.src = frame.url;
        view.image.classList.remove("hidden");
      } else {
        view.image.removeAttribute("src");
        view.image.classList.add("hidden");
      }
    }

    function ensureController() {
      if (previewController) return previewController;
      const api = globalThis.NvrCameraPreview;
      if (!api?.createCameraPreview) throw new Error("Camera preview support is unavailable.");
      previewController = api.createCameraPreview({ onState: renderState, onFrame: renderFrame });
      return previewController;
    }

    function mount(container, camera) {
      if (!container || !camera) return null;
      const cameraId = String(camera.id);
      if (view) {
        view.image.removeAttribute("src");
        view.image.classList.add("hidden");
      }
      view = null;
      const controllerForView = ensureController();
      if (camera.archived_at) controllerForView.select(null);
      else controllerForView.select(cameraId);

      const panel = document.createElement("section");
      panel.className = "camera-preview-panel";
      panel.dataset.cameraPreview = "";
      panel.setAttribute("aria-labelledby", `camera-preview-title-${++panelSequence}`);
      const heading = document.createElement("h4");
      heading.id = panel.getAttribute("aria-labelledby");
      heading.textContent = "Live preview";
      const status = document.createElement("p");
      status.dataset.previewStatus = "";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const image = document.createElement("img");
      image.className = "camera-preview-image hidden";
      image.alt = `Muted live preview for ${camera.name || "selected camera"}`;
      const actions = document.createElement("div");
      actions.className = "camera-preview-actions";
      const start = document.createElement("button");
      start.type = "button";
      start.textContent = "Show Preview";
      start.dataset.previewStart = "";
      start.setAttribute("aria-label", `Show muted live preview for ${camera.name || "selected camera"}`);
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "Retry Preview";
      retry.dataset.previewRetry = "";
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close Preview";
      close.dataset.previewClose = "";
      actions.append(start, retry, close);
      panel.append(heading, status, image, actions);
      container.replaceChildren();
      container.append(panel);

      view = { cameraId, container, status, image, start, retry, close };
      if (camera.archived_at) {
        status.textContent = "Archived cameras do not have a live preview.";
        status.dataset.state = "unavailable";
        start.disabled = true;
        start.hidden = true;
        retry.hidden = true;
        close.hidden = true;
      } else {
        start.addEventListener("click", () => controllerForView.start(cameraId));
        retry.addEventListener("click", () => controllerForView.retry());
        close.addEventListener("click", () => controllerForView.stop());
        const snapshot = controllerForView.snapshot();
        renderState(snapshot);
        if (snapshot.frameUrl) renderFrame({ cameraId, url: snapshot.frameUrl });
      }
      return panel;
    }

    function leave() {
      if (previewController) previewController.stop();
    }

    function clearSelection() {
      const priorView = view;
      view = null;
      if (priorView) {
        priorView.image.removeAttribute("src");
        priorView.image.classList.add("hidden");
      }
      if (previewController) previewController.select(null);
    }

    function close() {
      if (previewController) previewController.stop();
    }

    function retry() {
      return previewController?.retry() || false;
    }

    function snapshot() {
      return previewController?.snapshot() || { cameraId: null, state: "idle", message: "" };
    }

    return { mount, leave, clearSelection, close, retry, snapshot };
  }

  return { createCameraPreviewWorkspace };
});
