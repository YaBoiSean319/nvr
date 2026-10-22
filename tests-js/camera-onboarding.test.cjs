const test = require("node:test");
const assert = require("node:assert/strict");
const { buildCsvForStatuses, buildRetryCsv } = require("../app/static/camera-onboarding.js");

test("CSV retry keeps only failed records and preserves quoted commas and multiline fields", () => {
  const source = [
    "name,stream_url,group_name",
    'North,rtsp://198.51.100.10/main,"West, Lobby"',
    'South,rtsp://198.51.100.11/main,"Second line',
    'south"',
    "East,rtsp://198.51.100.12/main,East Wing",
  ].join("\n");

  const retry = buildRetryCsv(source, [
    { row: 2, status: "created" },
    { row: 3, status: "failed" },
    { row: 5, status: "duplicate" },
  ]);

  assert.equal(retry, [
    "name,stream_url,group_name",
    'South,rtsp://198.51.100.11/main,"Second line\nsouth"',
  ].join("\n"));
});

test("CSV retry rejects malformed quoting instead of silently selecting the wrong rows", () => {
  assert.throws(
    () => buildRetryCsv('name,stream_url\nBroken,"rtsp://camera.example/stream', [{ row: 2, status: "failed" }]),
    /malformed CSV/i,
  );
});

test("CSV apply includes only rows accepted in the reviewed preview", () => {
  const source = "name,stream_url\nNew camera,rtsp://198.51.100.20/main\nExisting,rtsp://198.51.100.21/main\nBroken,http://198.51.100.22/video";

  assert.equal(buildCsvForStatuses(source, [
    { row: 2, status: "accepted" },
    { row: 3, status: "duplicate" },
    { row: 4, status: "invalid" },
  ], ["accepted"]), "name,stream_url\nNew camera,rtsp://198.51.100.20/main");
});
