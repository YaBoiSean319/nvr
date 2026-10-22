(function installCameraDiscovery(root, createModule) {
  const discovery = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = discovery;
  root.NvrCameraDiscovery = discovery;
})(typeof window === "undefined" ? globalThis : window, function createCameraDiscovery() {
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  function createDiscoveryState() {
    return {
      candidates: [],
      cameras: [],
      selectedCandidateId: null,
      query: "",
      showLinked: false,
      refreshGeneration: 0,
    };
  }

  function candidateAddress(candidate) {
    for (const serviceUrl of candidate?.service_urls || []) {
      try {
        const address = new URL(serviceUrl).hostname;
        if (address) return address.replace(/^\[|\]$/g, "");
      } catch {}
    }
    return "";
  }

  function advertisedName(candidate) {
    for (const scope of candidate?.scopes || []) {
      if (typeof scope !== "string") continue;
      const match = /(?:^|\/)name\/([^/?#]+)/i.exec(scope);
      if (!match) continue;
      try {
        const name = decodeURIComponent(match[1]).trim();
        if (name) return name;
      } catch {
        const name = match[1].trim();
        if (name) return name;
      }
    }
    return "";
  }

  function candidateLabel(candidate) {
    const name = advertisedName(candidate);
    if (name) return name;
    const address = candidateAddress(candidate);
    if (String(candidate?.device_uuid || "").startsWith("ipscan:")) {
      return address ? "Device at " + address : "Address-based device";
    }
    return address ? "ONVIF device at " + address : "Unknown ONVIF device";
  }

  function visibleCandidates(state) {
    const query = String(state.query || "").trim().toLocaleLowerCase();
    return state.candidates.filter(candidate => {
      if (!state.showLinked && candidate.linked_camera_id) return false;
      if (!query) return true;
      const haystack = [candidateLabel(candidate), candidateAddress(candidate), candidate.device_uuid || ""].join(" ").toLocaleLowerCase();
      return haystack.includes(query);
    }).sort((left, right) => {
      const label = collator.compare(candidateLabel(left), candidateLabel(right));
      if (label) return label;
      const address = collator.compare(candidateAddress(left), candidateAddress(right));
      return address || collator.compare(String(left.id), String(right.id));
    });
  }

  function selectCandidate(state, candidateId) {
    if (!state.candidates.some(candidate => candidate.id === candidateId)) return false;
    state.selectedCandidateId = candidateId;
    return true;
  }

  function replaceResults(state, candidates, cameras) {
    state.candidates = Array.isArray(candidates) ? [...candidates] : [];
    state.cameras = Array.isArray(cameras) ? [...cameras] : [];
    if (state.selectedCandidateId && !state.candidates.some(candidate => candidate.id === state.selectedCandidateId)) {
      state.selectedCandidateId = null;
    }
  }

  function beginRefresh(state) {
    state.refreshGeneration += 1;
    return state.refreshGeneration;
  }

  function isCurrentRefresh(state, generation) {
    return generation === state.refreshGeneration;
  }

  function linkedCamera(state, candidate) {
    if (!candidate?.linked_camera_id) return null;
    return state.cameras.find(camera => camera.id === candidate.linked_camera_id) || null;
  }

  return {
    beginRefresh,
    candidateAddress,
    candidateLabel,
    createDiscoveryState,
    isCurrentRefresh,
    linkedCamera,
    replaceResults,
    selectCandidate,
    visibleCandidates,
  };
});
