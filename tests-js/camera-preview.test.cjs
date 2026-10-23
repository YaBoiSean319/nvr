const test = require("node:test");
const assert = require("node:assert/strict");
const { createCameraPreview } = require("../app/static/camera-preview.js");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function fakeStream() {
  let pendingRead = null;
  let cancelled = false;
  return {
    reader: {
      read() {
        if (cancelled) return Promise.resolve({ done: true });
        pendingRead = deferred();
        return pendingRead.promise;
      },
      cancel() {
        cancelled = true;
        pendingRead?.resolve({ done: true });
        return Promise.resolve();
      },
    },
    push(value) { pendingRead?.resolve({ done: false, value: Uint8Array.from(value) }); },
    get cancelled() { return cancelled; },
  };
}

test("preview requests start only when explicitly requested and use authenticated low-quality delivery", async () => {
  const calls = [];
  const stream = fakeStream();
  const preview = createCameraPreview({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, body: { getReader: () => stream.reader } };
    },
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
  });

  assert.deepEqual(calls, []);
  assert.equal(preview.snapshot().state, "idle");
  preview.start("camera/one");
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/cameras/camera%2Fone/live?quality=low");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.ok(calls[0].options.signal instanceof AbortSignal);
  assert.equal(preview.snapshot().cameraId, "camera/one");
  assert.equal(preview.snapshot().state, "loading");
  preview.stop();
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(stream.cancelled, true);
  assert.equal(preview.snapshot().state, "idle");
});

test("switching selection stops the old preview and never autostarts the new camera", async () => {
  const calls = [];
  const streams = [fakeStream(), fakeStream()];
  let index = 0;
  const preview = createCameraPreview({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, body: { getReader: () => streams[index++].reader } };
    },
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  preview.select("camera-b");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(streams[0].cancelled, true);
  assert.equal(preview.snapshot().cameraId, "camera-b");
  assert.equal(preview.snapshot().state, "idle");
  preview.start();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /camera-b/);
  assert.equal(calls[1].options.signal.aborted, false);
  preview.stop();
});

test("a decoded frame marks the preview fresh and its object URL is released on close", async () => {
  const stream = fakeStream();
  const frames = [];
  const revoked = [];
  const preview = createCameraPreview({
    fetchImpl: async () => ({ ok: true, body: { getReader: () => stream.reader } }),
    createObjectUrl: blob => `blob:${blob.size}`,
    revokeObjectUrl: url => revoked.push(url),
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
    onFrame: frame => frames.push(frame),
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  stream.push([0xff, 0xd8, 0x01, 0x02, 0xff, 0xd9]);
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(preview.snapshot().state, "fresh");
  assert.equal(frames[0].cameraId, "camera-a");
  assert.equal(frames[0].url, "blob:6");
  preview.stop();
  assert.deepEqual(revoked, ["blob:6"]);
  assert.equal(frames.at(-1).url, null);
});

test("an unsupported browser response is visible and can be retried without leaving a stream running", async () => {
  let calls = 0;
  const preview = createCameraPreview({
    fetchImpl: async () => {
      calls++;
      return { ok: false, status: 503, json: async () => ({ detail: "Live video transcoding is unavailable" }) };
    },
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(preview.snapshot().state, "unsupported");
  assert.equal(preview.retry(), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(preview.snapshot().state, "unsupported");
  preview.stop();
});

test("the existing live status endpoint changes a frame from fresh to stale", async () => {
  const stream = fakeStream();
  const intervalCallbacks = [];
  let now = 1000;
  const preview = createCameraPreview({
    fetchImpl: async url => url.endsWith("/live/status")
      ? { ok: true, json: async () => ({ state: "stale", active_viewers: 1 }) }
      : { ok: true, body: { getReader: () => stream.reader } },
    setIntervalImpl: callback => { intervalCallbacks.push(callback); return intervalCallbacks.length; },
    clearIntervalImpl: () => {},
    now: () => now,
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  stream.push([0xff, 0xd8, 0x01, 0xff, 0xd9]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(preview.snapshot().state, "fresh");

  now += 13000;
  intervalCallbacks[0]();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(preview.snapshot().state, "stale");
  preview.stop();
});

test("a late response from a previous camera is ignored and its body is canceled", async () => {
  const request = deferred();
  let oldBodyCanceled = false;
  const preview = createCameraPreview({
    fetchImpl: (url) => url.includes("camera-a") ? request.promise : new Promise(() => {}),
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  preview.select("camera-b");
  request.resolve({ ok: true, body: { cancel: async () => { oldBodyCanceled = true; } } });
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(oldBodyCanceled, true);
  assert.equal(preview.snapshot().cameraId, "camera-b");
  assert.equal(preview.snapshot().state, "idle");
  preview.stop();
});

test("capacity failures remain failed instead of being mislabeled as unsupported or recording health", async () => {
  const preview = createCameraPreview({
    fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ detail: "All live-view slots are in use; close another view and retry" }) }),
    setIntervalImpl: () => 1,
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(preview.snapshot().state, "failed");
  assert.match(preview.snapshot().message, /slots are in use/);
  preview.stop();
});

test("an expired authenticated session stops preview media when status checks are unauthorized", async () => {
  const stream = fakeStream();
  const intervalCallbacks = [];
  let mediaSignal;
  const preview = createCameraPreview({
    fetchImpl: async (url, options) => {
      if (url.endsWith("/live/status")) return { ok: false, status: 401, json: async () => ({ detail: "Sign in again" }) };
      mediaSignal = options.signal;
      return { ok: true, body: { getReader: () => stream.reader } };
    },
    setIntervalImpl: callback => { intervalCallbacks.push(callback); return intervalCallbacks.length; },
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  intervalCallbacks[0]();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(mediaSignal.aborted, true);
  assert.equal(stream.cancelled, true);
  assert.equal(preview.snapshot().state, "failed");
  preview.stop();
});

test("a removed or archived camera releases its active frame and media reader", async () => {
  const stream = fakeStream();
  const intervalCallbacks = [];
  const revoked = [];
  const preview = createCameraPreview({
    fetchImpl: async url => url.endsWith("/live/status")
      ? { ok: false, status: 410, json: async () => ({ detail: "Camera is archived" }) }
      : { ok: true, body: { getReader: () => stream.reader } },
    createObjectUrl: blob => `blob:${blob.size}`,
    revokeObjectUrl: url => revoked.push(url),
    setIntervalImpl: callback => { intervalCallbacks.push(callback); return intervalCallbacks.length; },
    clearIntervalImpl: () => {},
  });

  preview.start("camera-a");
  await new Promise(resolve => setImmediate(resolve));
  stream.push([0xff, 0xd8, 0x01, 0xff, 0xd9]);
  await new Promise(resolve => setImmediate(resolve));
  intervalCallbacks[0]();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(preview.snapshot().state, "unavailable");
  assert.equal(preview.snapshot().frameUrl, null);
  assert.deepEqual(revoked, ["blob:5"]);
  assert.equal(stream.cancelled, true);
  preview.stop();
});
