const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createDiscoveryState,
  candidateAddress,
  candidateLabel,
  visibleCandidates,
  selectCandidate,
  replaceResults,
  beginRefresh,
  isCurrentRefresh,
  mountWorkspace,
} = require("../app/static/camera-discovery.js");

const candidates = [
  {
    id: "linked",
    device_uuid: "11111111-1111-4111-8111-111111111111",
    service_urls: ["http://192.0.2.22/onvif/device_service"],
    scopes: ["onvif://www.onvif.org/name/Loading%20Bay"],
    linked_camera_id: "camera-22",
  },
  {
    id: "unlinked-b",
    device_uuid: "22222222-2222-4222-8222-222222222222",
    service_urls: ["http://192.0.2.12/onvif/device_service"],
    scopes: ["onvif://www.onvif.org/name/West%20Door"],
    linked_camera_id: null,
  },
  {
    id: "unlinked-a",
    device_uuid: "33333333-3333-4333-8333-333333333333",
    service_urls: ["http://192.0.2.12/onvif/device_service"],
    scopes: ["onvif://www.onvif.org/name/West Door"],
    linked_camera_id: null,
  },
];

test("discovery candidates default to unlinked and can reveal linked devices explicitly", () => {
  const state = createDiscoveryState();
  replaceResults(state, candidates, [{ id: "camera-22", name: "Loading Bay" }]);

  assert.deepEqual(visibleCandidates(state).map(candidate => candidate.id), ["unlinked-a", "unlinked-b"]);

  state.showLinked = true;
  assert.deepEqual(visibleCandidates(state).map(candidate => candidate.id), ["linked", "unlinked-a", "unlinked-b"]);
});

test("discovery search matches advertised names and endpoint addresses with stable ties", () => {
  const state = createDiscoveryState();
  replaceResults(state, candidates, []);
  state.query = "west door";
  assert.deepEqual(visibleCandidates(state).map(candidate => candidate.id), ["unlinked-a", "unlinked-b"]);
  state.query = "192.0.2.22";
  state.showLinked = true;
  assert.deepEqual(visibleCandidates(state).map(candidate => candidate.id), ["linked"]);
});

test("candidate address and label handle incomplete device identity without inventing a name", () => {
  assert.equal(candidateAddress({ service_urls: ["http://192.0.2.44/onvif/device_service"] }), "192.0.2.44");
  assert.equal(candidateLabel({
    device_uuid: "ipscan:hashed-endpoint",
    service_urls: ["http://192.0.2.44/onvif/device_service"],
    scopes: [],
  }), "Device at 192.0.2.44");
  assert.equal(candidateLabel({
    device_uuid: "44444444-4444-4444-8444-444444444444",
    service_urls: [],
    scopes: [],
  }), "Unknown ONVIF device");
  assert.equal(candidateLabel({
    device_uuid: "ipscan:hashed-endpoint",
    service_urls: [],
    scopes: [],
  }), "Address-based device");
});

test("selected candidate survives refresh while present and clears when no longer available", () => {
  const state = createDiscoveryState();
  replaceResults(state, candidates, []);
  assert.equal(selectCandidate(state, "unlinked-b"), true);
  replaceResults(state, [...candidates].reverse(), []);
  assert.equal(state.selectedCandidateId, "unlinked-b");
  replaceResults(state, candidates.filter(candidate => candidate.id !== "unlinked-b"), []);
  assert.equal(state.selectedCandidateId, null);
  assert.equal(selectCandidate(state, "missing"), false);
});

test("only the newest discovery refresh may update the view", () => {
  const state = createDiscoveryState();
  const first = beginRefresh(state);
  const second = beginRefresh(state);
  assert.equal(isCurrentRefresh(state, first), false);
  assert.equal(isCurrentRefresh(state, second), true);
});

test("discovery module exposes the workspace controller seam", () => {
  assert.equal(typeof mountWorkspace, "function");
  assert.equal(mountWorkspace(null, {}), null);
});
