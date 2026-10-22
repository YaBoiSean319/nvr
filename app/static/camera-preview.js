(function installCameraPreview(root, createModule) {
  const preview = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = preview;
  root.NvrCameraPreview = preview;
})(typeof window === "undefined" ? globalThis : window, function createCameraPreviewModule() {
  const MAX_FRAME_BYTES = 4 * 1024 * 1024;

  function createCameraPreview(options = {}) {
    const fetchImpl = options.fetchImpl || globalThis.fetch.bind(globalThis);
    const createObjectUrl = options.createObjectUrl || (blob => URL.createObjectURL(blob));
    const revokeObjectUrl = options.revokeObjectUrl || (url => URL.revokeObjectURL(url));
    const setIntervalImpl = options.setIntervalImpl || globalThis.setInterval.bind(globalThis);
    const clearIntervalImpl = options.clearIntervalImpl || globalThis.clearInterval.bind(globalThis);
    const setTimeoutImpl = options.setTimeoutImpl || globalThis.setTimeout.bind(globalThis);
    const clearTimeoutImpl = options.clearTimeoutImpl || globalThis.clearTimeout.bind(globalThis);
    const now = options.now || Date.now;
    const onState = options.onState || (() => {});
    const onFrame = options.onFrame || (() => {});
    const statusPollMs = options.statusPollMs || 5000;
    const freshMs = options.freshMs || 12000;
    let selectedCameraId = null;
    let active = null;

    function currentSnapshot() {
      if (!active) return { cameraId: selectedCameraId, state: "idle", message: "" };
      return {
        cameraId: active.cameraId,
        state: active.state,
        message: active.message || "",
        lastFrameAt: active.lastFrameAt,
        frameUrl: active.objectUrl,
      };
    }

    function emitState(context, state, message = "") {
      if (active !== context) return;
      context.state = state;
      context.message = message;
      try { onState(currentSnapshot()); } catch { /* UI observers cannot break media cleanup. */ }
    }

    function emitIdle(cameraId) {
      try { onState({ cameraId, state: "idle", message: "" }); } catch { /* UI observers cannot break media cleanup. */ }
    }

    function emitFrame(context, url) {
      if (active !== context) return;
      try { onFrame({ cameraId: context.cameraId, url, lastFrameAt: context.lastFrameAt }); } catch { /* UI observers cannot break media cleanup. */ }
    }

    function isCurrent(context) {
      return active === context && !context.controller.signal.aborted;
    }

    function clearStatusTimer(context) {
      if (context.statusTimer !== null) clearIntervalImpl(context.statusTimer);
      context.statusTimer = null;
    }

    function cancelReader(context) {
      const reader = context.reader;
      context.reader = null;
      if (reader && typeof reader.cancel === "function") {
        Promise.resolve(reader.cancel()).catch(() => {});
      }
    }

    function clearFrame(context) {
      if (context.objectUrl) revokeObjectUrl(context.objectUrl);
      context.objectUrl = null;
      context.lastFrameAt = null;
      try { onFrame({ cameraId: context.cameraId, url: null, lastFrameAt: null }); } catch { /* No-op during resource release. */ }
    }

    function stopActive() {
      const context = active;
      if (!context) return false;
      active = null;
      clearStatusTimer(context);
      if (context.retryTimer !== null) clearTimeoutImpl(context.retryTimer);
      context.retryTimer = null;
      context.controller.abort();
      cancelReader(context);
      if (context.objectUrl) revokeObjectUrl(context.objectUrl);
      context.objectUrl = null;
      emitIdle(selectedCameraId);
      try { onFrame({ cameraId: context.cameraId, url: null, lastFrameAt: null }); } catch { /* No-op during teardown. */ }
      return true;
    }

    function classifyFailure(status, detail) {
      const message = String(detail || "Live preview could not start");
      if (status === 404 || status === 410) return { state: "unavailable", message };
      if (/transcod(?:ing|er).*unavailable|unsupported browser|cannot read the live stream/i.test(message)) {
        return { state: "unsupported", message };
      }
      return { state: "failed", message };
    }

    function terminal(context, state, message) {
      if (active !== context) return;
      context.terminal = true;
      clearStatusTimer(context);
      if (context.retryTimer !== null) clearTimeoutImpl(context.retryTimer);
      context.retryTimer = null;
      context.controller.abort();
      cancelReader(context);
      clearFrame(context);
      emitState(context, state, message);
    }

    function retryDelay(context, delayMs) {
      if (!isCurrent(context)) return Promise.resolve();
      return new Promise(resolve => {
        const finish = () => {
          if (context.retryTimer !== null) clearTimeoutImpl(context.retryTimer);
          context.retryTimer = null;
          context.controller.signal.removeEventListener("abort", finish);
          resolve();
        };
        context.retryTimer = setTimeoutImpl(finish, delayMs);
        context.controller.signal.addEventListener("abort", finish, { once: true });
      });
    }

    function consumeFrames(context, chunk, buffer) {
      const merged = new Uint8Array(buffer.length + chunk.length);
      merged.set(buffer);
      merged.set(chunk, buffer.length);
      let remaining = merged;
      while (true) {
        let start = -1;
        let end = -1;
        for (let index = 0; index < remaining.length - 1; index++) {
          if (remaining[index] === 0xff && remaining[index + 1] === 0xd8) { start = index; break; }
        }
        if (start < 0) return remaining.slice(Math.max(0, remaining.length - 1));
        for (let index = start + 2; index < remaining.length - 1; index++) {
          if (remaining[index] === 0xff && remaining[index + 1] === 0xd9) { end = index + 2; break; }
        }
        if (end < 0) {
          const partial = remaining.slice(start);
          if (partial.length > MAX_FRAME_BYTES) throw new Error("Preview frame exceeded the size limit");
          return partial;
        }
        const frame = remaining.slice(start, end);
        const objectUrl = createObjectUrl(new Blob([frame], { type: "image/jpeg" }));
        const previousUrl = context.objectUrl;
        context.objectUrl = objectUrl;
        context.lastFrameAt = now();
        context.retryCount = 0;
        emitFrame(context, objectUrl);
        emitState(context, "fresh");
        if (previousUrl) revokeObjectUrl(previousUrl);
        remaining = remaining.slice(end);
      }
    }

    async function connect(context) {
      while (isCurrent(context)) {
        try {
          emitState(context, context.lastFrameAt ? "stale" : "loading");
          const response = await fetchImpl(`/api/cameras/${encodeURIComponent(context.cameraId)}/live?quality=low`, {
            credentials: "same-origin",
            signal: context.controller.signal,
          });
          if (!isCurrent(context)) {
            if (typeof response.body?.cancel === "function") response.body.cancel().catch(() => {});
            return;
          }
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            const failure = classifyFailure(response.status, body.detail);
            terminal(context, failure.state, failure.message);
            return;
          }
          if (!response.body || typeof response.body.getReader !== "function") {
            terminal(context, "unsupported", "This browser cannot read the live preview stream");
            return;
          }
          const reader = response.body.getReader();
          context.reader = reader;
          let buffer = new Uint8Array(0);
          context.retryCount = 0;
          while (isCurrent(context)) {
            const result = await reader.read();
            if (!isCurrent(context)) return;
            if (result.done) throw new Error("The camera preview stream ended");
            buffer = consumeFrames(context, result.value || new Uint8Array(0), buffer);
            if (buffer.length > MAX_FRAME_BYTES) throw new Error("Preview stream exceeded the frame buffer limit");
          }
          return;
        } catch (error) {
          if (!isCurrent(context)) return;
          if (error?.name === "NotSupportedError") {
            terminal(context, "unsupported", error.message || "This browser cannot display the live preview");
            return;
          }
          const delay = Math.min(1000 * (2 ** context.retryCount), 8000);
          context.retryCount = Math.min(context.retryCount + 1, 4);
          emitState(context, context.lastFrameAt ? "stale" : "reconnecting", context.lastFrameAt ? "The last frame is being retained while the preview reconnects." : "Reconnecting to the camera preview.");
          cancelReader(context);
          await retryDelay(context, delay);
        } finally {
          if (context.reader && !isCurrent(context)) cancelReader(context);
        }
      }
    }

    async function pollStatus(context) {
      if (!isCurrent(context) || context.statusPending) return;
      context.statusPending = true;
      try {
        const response = await fetchImpl(`/api/cameras/${encodeURIComponent(context.cameraId)}/live/status`, {
          credentials: "same-origin",
          signal: context.controller.signal,
        });
        if (!isCurrent(context)) return;
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          const failure = classifyFailure(response.status, body.detail);
          if ([401, 403].includes(response.status) || failure.state === "unavailable") terminal(context, failure.state, failure.message);
          else if (context.lastFrameAt && now() - context.lastFrameAt > freshMs) emitState(context, "stale", "The latest frame is no longer fresh.");
          else emitState(context, "lost", failure.message);
          return;
        }
        const status = await response.json();
        if (!isCurrent(context)) return;
        const age = context.lastFrameAt === null ? null : now() - context.lastFrameAt;
        if (age !== null && age <= freshMs && status.state === "live") emitState(context, "fresh");
        else if (age !== null || status.state === "stale") emitState(context, "stale", "The last preview frame is stale.");
        else emitState(context, "reconnecting", "Waiting for a fresh preview frame.");
      } catch (error) {
        if (!isCurrent(context)) return;
        emitState(context, context.lastFrameAt ? "stale" : "lost", error.message || "Preview status is unavailable.");
      } finally {
        context.statusPending = false;
      }
    }

    function start(cameraId = selectedCameraId) {
      const id = String(cameraId || "").trim();
      if (!id) return false;
      if (selectedCameraId !== id) select(id);
      if (active?.cameraId === id) {
        if (active.terminal) return retry();
        return false;
      }
      if (active) stopActive();
      const context = {
        cameraId: id,
        controller: new AbortController(),
        state: "loading",
        message: "",
        objectUrl: null,
        lastFrameAt: null,
        statusTimer: null,
        retryTimer: null,
        reader: null,
        retryCount: 0,
        terminal: false,
        statusPending: false,
      };
      active = context;
      emitState(context, "loading");
      context.statusTimer = setIntervalImpl(() => { void pollStatus(context); }, statusPollMs);
      void connect(context);
      return true;
    }

    function select(cameraId) {
      const id = cameraId ? String(cameraId).trim() : null;
      if (selectedCameraId === id) return false;
      selectedCameraId = id;
      if (active && active.cameraId !== id) stopActive();
      if (!active) emitIdle(selectedCameraId);
      return true;
    }

    function stop() {
      return stopActive();
    }

    function retry() {
      if (!selectedCameraId) return false;
      stopActive();
      return start(selectedCameraId);
    }

    return { start, stop, select, retry, snapshot: currentSnapshot };
  }

  return { createCameraPreview };
});
