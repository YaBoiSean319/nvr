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


  function mountWorkspace(root, services = {}) {
    if (!root) return null;
    const state = createDiscoveryState();
    const { api, refreshApp, formatDateTime, guardDraftsIn, clearConfigDraft, inspectCameraFromStatus } = services;
    let discoverySelectionGeneration = 0;
    let discoveryScanPending = false;
    let discoveryLastScanText = "No scan has run in this session.";
    let discoveryLastScanRequest = null;
    let discoveryRenderedCandidateId = null;
    let discoverySelectedSnapshot = null;
    let discoveryResultsLoaded = false;
    const discoveryAddPendingIds = new Set();
function ensureDiscoveryWorkspace() {
  if(!root||root.dataset.discoveryWorkspaceReady==="true")return;
  const heading=root.querySelector("#discovery-heading")||document.createElement("h3");
  heading.id="discovery-heading";heading.textContent="Discover cameras";
  const description=root.querySelector("p")||document.createElement("p");
  description.textContent="Use multicast on the camera VLAN or scan a routed private IPv4 range. Devices stay candidates until an administrator reviews and tests one; scanning never starts recording.";
  const ipLabel=root.querySelector('label[for="discovery-ip-range"]')||document.createElement("label");ipLabel.htmlFor="discovery-ip-range";ipLabel.textContent="Private IPv4 range (CIDR)";
  const ipRange=root.querySelector("#discovery-ip-range")||document.createElement("input");ipRange.id="discovery-ip-range";ipRange.inputMode="numeric";ipRange.autocomplete="off";ipRange.placeholder="192.168.1.0/24";
  const timeoutLabel=root.querySelector('label[for="discovery-timeout"]')||document.createElement("label");timeoutLabel.htmlFor="discovery-timeout";timeoutLabel.textContent="VLAN scan duration (seconds)";
  const timeout=root.querySelector("#discovery-timeout")||document.createElement("input");timeout.id="discovery-timeout";timeout.type="number";timeout.min="0.5";timeout.max="15";timeout.step="0.5";timeout.value=timeout.value||"5";
  const vlanButton=root.querySelector("#discover-cameras")||document.createElement("button");vlanButton.id="discover-cameras";vlanButton.type="button";vlanButton.textContent="Scan camera VLAN";
  const rangeButton=root.querySelector("#discover-ip-range")||document.createElement("button");rangeButton.id="discover-ip-range";rangeButton.type="button";rangeButton.textContent="Scan private range";
  const message=root.querySelector("#discovery-message")||document.createElement("p");message.id="discovery-message";message.setAttribute("role","status");message.setAttribute("aria-live","polite");
  const candidates=root.querySelector("#discovery-candidates")||document.createElement("div");candidates.id="discovery-candidates";
  const scanControls=document.createElement("div");scanControls.className="discovery-scan-toolbar";scanControls.setAttribute("aria-label","Discovery scan controls");
  const vlanScan=document.createElement("div");vlanScan.className="discovery-scan-action";vlanScan.append(vlanButton);
  const rangeScan=document.createElement("div");rangeScan.className="discovery-range-scan";
  const rangeField=document.createElement("div");rangeField.className="discovery-scan-field";rangeField.append(ipLabel,ipRange);rangeScan.append(rangeField,rangeButton);
  const advanced=document.createElement("details");advanced.className="discovery-scan-advanced";
  const advancedSummary=document.createElement("summary");advancedSummary.textContent="VLAN scan timing";advanced.append(advancedSummary);
  const timingField=document.createElement("div");timingField.className="discovery-scan-field";timingField.append(timeoutLabel,timeout);
  const timingNote=document.createElement("p");timingNote.textContent="VLAN scans are bounded to 0.5–15 seconds. Private-range scans use unicast ONVIF probes and are limited to 256 addresses per scan.";advanced.append(timingField,timingNote);
  const retryScan=document.createElement("button");retryScan.id="discovery-retry-scan";retryScan.type="button";retryScan.textContent="Retry last scan";retryScan.className="hidden";
  retryScan.addEventListener("click",()=>{if(discoveryLastScanRequest)void runDiscoveryScan(discoveryLastScanRequest.ip_range,discoveryLastScanRequest.timeout_seconds);});
  scanControls.append(vlanScan,rangeScan,advanced,retryScan);
  const lastScan=document.createElement("p");lastScan.id="discovery-last-scan";lastScan.className="discovery-last-scan";lastScan.setAttribute("role","status");lastScan.textContent=discoveryLastScanText;
  const toolbar=document.createElement("div");toolbar.className="discovery-results-toolbar";
  const searchLabel=document.createElement("label");searchLabel.htmlFor="discovery-search";searchLabel.textContent="Search discovered devices";
  const search=document.createElement("input");search.id="discovery-search";search.type="search";search.autocomplete="off";search.placeholder="Name, address, or identity";
  search.addEventListener("input",()=>{state.query=search.value;renderDiscoveryResults();});
  const linkedLabel=document.createElement("label");linkedLabel.className="discovery-linked-filter";linkedLabel.htmlFor="discovery-show-linked";linkedLabel.textContent="Show added devices";
  const showLinked=document.createElement("input");showLinked.id="discovery-show-linked";showLinked.type="checkbox";
  linkedLabel.append(showLinked);
  showLinked.addEventListener("change",()=>{state.showLinked=showLinked.checked;renderDiscoveryResults();});
  const refreshButton=document.createElement("button");refreshButton.type="button";refreshButton.textContent="Refresh results";refreshButton.addEventListener("click",()=>void refreshDiscovery());
  toolbar.append(searchLabel,search,linkedLabel,refreshButton);
  const count=document.createElement("p");count.className="discovery-result-count";count.dataset.discoveryCount="";count.setAttribute("aria-live","polite");
  const layout=document.createElement("div");layout.className="discovery-layout";
  const resultsPane=document.createElement("section");resultsPane.className="discovery-results-pane";resultsPane.setAttribute("aria-labelledby","discovery-results-heading");
  const resultsHeading=document.createElement("h4");resultsHeading.id="discovery-results-heading";resultsHeading.textContent="Discovered devices";
  const tableScroll=document.createElement("div");tableScroll.className="discovery-table-scroll";tableScroll.id="discovery-table-scroll";tableScroll.tabIndex=0;tableScroll.setAttribute("role","region");tableScroll.setAttribute("aria-label","Discovered devices results");
  tableScroll.textContent="Loading discovered devices…";count.textContent="Loading discovery results…";
  resultsPane.append(resultsHeading,count,tableScroll);
  const selectedPane=document.createElement("section");selectedPane.id="discovery-device-panel";selectedPane.className="discovery-selected-pane";selectedPane.setAttribute("aria-labelledby","discovery-selected-heading");
  layout.append(resultsPane,selectedPane);
  candidates.className="discovery-workspace";candidates.replaceChildren(toolbar,layout);
  root.replaceChildren(heading,description,scanControls,lastScan,message,candidates);
  root.dataset.discoveryWorkspaceReady="true";
}

function discoveryStatusMessage(messageText) {
  const target=root.querySelector("#discovery-message");if(target)target.textContent=messageText;
}

function safeDiscoveryError(error, sensitiveValues=[]) {
  let message=String(error?.message||"Discovery request failed");
  for(const value of sensitiveValues){if(value)message=message.split(String(value)).join("[redacted]");}
  return message;
}

function safeDiscoveredEndpoint(value) {
  try {
    const url=new URL(value);
    if(!["http:","https:"].includes(url.protocol)||!url.hostname)return "";
    url.username="";url.password="";url.search="";url.hash="";
    return url.toString();
  } catch { return ""; }
}

function discoveryEndpointUrl(value) {
  try {
    const url=new URL(value);
    if(!["http:","https:"].includes(url.protocol)||!url.hostname||url.username||url.password||url.search||url.hash)throw new Error();
    return url.toString();
  } catch { throw new Error("Use an HTTP(S) device endpoint without embedded credentials, a query string, or a fragment."); }
}

function renderDiscoveryResults() {
  const scroll=root.querySelector("#discovery-table-scroll"),count=root.querySelector("[data-discovery-count]");
  if(!scroll||!count)return;
  const rows=window.NvrCameraDiscovery.visibleCandidates(state);
  const allUnlinked=state.candidates.filter(candidate=>!candidate.linked_camera_id).length;
  const allLinked=state.candidates.length-allUnlinked;
  count.textContent=state.showLinked
    ? `${rows.length} shown · ${allUnlinked} not added · ${allLinked} already added`
    : `${rows.length} not-added device${rows.length===1?"":"s"} shown${allLinked?` · ${allLinked} added hidden`:""}`;
  if(!rows.length){
    const empty=document.createElement("p");empty.className="discovery-empty-state";
    empty.textContent=state.candidates.length
      ? "No discovered devices match this search and filter."
      : "No discovered devices yet. Start a VLAN scan or scan a private IPv4 range.";
    scroll.replaceChildren(empty);return;
  }
  const table=document.createElement("table");table.className="discovery-table";table.setAttribute("aria-label","Discovered cameras");
  const head=document.createElement("thead"),headRow=document.createElement("tr");
  for(const label of ["Device","Address","Identity","State","Last seen"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;headRow.append(cell);}
  head.append(headRow);table.append(head);
  const body=document.createElement("tbody");
  for(const candidate of rows){
    const row=document.createElement("tr");row.classList.toggle("selected-discovery-candidate",candidate.id===state.selectedCandidateId);
    const nameCell=document.createElement("th");nameCell.scope="row";
    const select=document.createElement("button");select.type="button";select.className="discovery-candidate-select";select.dataset.discoveryCandidateId=candidate.id;select.textContent=window.NvrCameraDiscovery.candidateLabel(candidate);select.setAttribute("aria-pressed",String(candidate.id===state.selectedCandidateId));
    select.addEventListener("click",()=>void selectDiscoveryCandidate(candidate.id));nameCell.append(select);row.append(nameCell);
    const address=document.createElement("td");address.textContent=window.NvrCameraDiscovery.candidateAddress(candidate)||"Not reported";row.append(address);
    const identity=document.createElement("td");identity.textContent=String(candidate.device_uuid||"").startsWith("ipscan:")?"Not advertised":candidate.device_uuid||"Not advertised";row.append(identity);
    const stateCell=document.createElement("td"),linked=window.NvrCameraDiscovery.linkedCamera(state,candidate);
    stateCell.textContent=candidate.linked_camera_id?`Already added${linked?` · ${linked.name}`:""}`:candidate.add_in_progress?"Add in progress":"Not added";row.append(stateCell);
    const seen=document.createElement("td");seen.textContent=candidate.last_seen_at?formatDateTime(candidate.last_seen_at,true):"Unknown";row.append(seen);body.append(row);
  }
  table.append(body);scroll.replaceChildren(table);
}

function renderDiscoverySelection() {
  const target=root.querySelector("#discovery-device-panel");if(!target)return;
  const candidate=state.candidates.find(item=>item.id===state.selectedCandidateId)||discoverySelectedSnapshot;
  if(!candidate){
    discoveryRenderedCandidateId=null;
    const empty=document.createElement("div");empty.className="discovery-selected-empty";
    const heading=document.createElement("h4");heading.id="discovery-selected-heading";heading.textContent="Device setup";
    const note=document.createElement("p");note.textContent="Select a discovered device to review its identity, load stream profiles, test the connection, or link it to an existing camera.";
    empty.append(heading,note);target.replaceChildren(empty);return;
  }
  if(discoveryRenderedCandidateId!==candidate.id){
    discoveryRenderedCandidateId=candidate.id;target.replaceChildren();
    const heading=document.createElement("h4");heading.id="discovery-selected-heading";heading.textContent="Selected device";
    const label=document.createElement("strong");label.className="discovery-selected-name";label.textContent=window.NvrCameraDiscovery.candidateLabel(candidate);
    const address=document.createElement("p");address.className="discovery-selected-address";address.textContent=`Address · ${window.NvrCameraDiscovery.candidateAddress(candidate)||"Not reported"}`;
    const identity=document.createElement("p");identity.className="discovery-selected-identity";identity.textContent=String(candidate.device_uuid||"").startsWith("ipscan:")?"ONVIF identity was not advertised. This result is tracked by an address-based fingerprint.":`ONVIF identity · ${candidate.device_uuid}`;
    const candidateStatus=document.createElement("p");candidateStatus.className="discovery-candidate-state";candidateStatus.dataset.discoveryCandidateState="";candidateStatus.setAttribute("role","status");
    const feedback=document.createElement("p");feedback.className="discovery-device-message";feedback.id="discovery-device-message";feedback.setAttribute("role","status");feedback.setAttribute("aria-live","polite");
    target.append(heading,label,address,identity);
    const endpoints=(candidate.service_urls||[]).map(safeDiscoveredEndpoint).filter(Boolean);
    if(endpoints.length){
      const endpointDetails=document.createElement("details"),endpointSummary=document.createElement("summary"),endpointList=document.createElement("ul");
      endpointDetails.className="discovery-endpoint-details";endpointSummary.textContent=`${endpoints.length} advertised ONVIF endpoint${endpoints.length===1?"":"s"}`;
      for(const endpoint of endpoints){const item=document.createElement("li");item.textContent=endpoint;endpointList.append(item);}
      endpointDetails.append(endpointSummary,endpointList);target.append(endpointDetails);
    }
    const capabilities=candidate.capabilities&&typeof candidate.capabilities==="object"?Object.entries(candidate.capabilities):[];
    if(capabilities.length){
      const capabilityDetails=document.createElement("details"),capabilitySummary=document.createElement("summary"),list=document.createElement("ul");
      capabilityDetails.className="discovery-capabilities";capabilitySummary.textContent="Reported capabilities";
      for(const [name,value] of capabilities){const item=document.createElement("li");item.textContent=`${name.replaceAll("_"," ")} · ${String(value||"unknown")}`;list.append(item);}
      capabilityDetails.append(capabilitySummary,list);target.append(capabilityDetails);
    }
    target.append(candidateStatus,feedback);
    const current=state.candidates.find(item=>item.id===candidate.id);
    if(!current){candidateStatus.textContent="This device is no longer in the current discovery results. Refresh the results before continuing.";updateDiscoverySelectedStatus(candidate);return;}
    if(current.linked_camera_id){
      const linked=window.NvrCameraDiscovery.linkedCamera(state,current);
      candidateStatus.textContent=linked?`Already added as ${linked.name}. Duplicate addition is disabled.`:"Already linked to an existing camera. Duplicate addition is disabled.";
      if(linked){const open=document.createElement("button");open.type="button";open.className="discovery-open-camera";open.dataset.discoveryOpenExisting="";open.textContent="Open existing camera";open.onclick=()=>void inspectCameraFromStatus(linked.id);target.append(open);}
      return;
    }
    target.append(buildDiscoveryCandidateSetup(current));
  }
  updateDiscoverySelectedStatus(state.candidates.find(item=>item.id===candidate.id)||candidate);
}

function updateDiscoverySelectedStatus(candidate) {
  const candidateStatus=root.querySelector("[data-discovery-candidate-state]");if(!candidateStatus)return;
  const current=state.candidates.find(item=>item.id===candidate.id);
  const linked=current&&window.NvrCameraDiscovery.linkedCamera(state,current);
  if(!current){candidateStatus.textContent="This device is no longer in the current discovery results. Its draft is kept here but cannot be submitted.";}
  else if(current.linked_camera_id){candidateStatus.textContent=linked?`Already added as ${linked.name}. Your current draft is preserved, but duplicate addition is disabled.`:"Already linked to an existing camera. Your current draft is preserved, but duplicate addition is disabled.";}
  else if(current.add_in_progress)candidateStatus.textContent="Another request is testing or adding this device. Wait for it to finish before retrying.";
  else candidateStatus.textContent=`First seen ${current.first_seen_at?formatDateTime(current.first_seen_at):"unknown"} · Last seen ${current.last_seen_at?formatDateTime(current.last_seen_at):"unknown"}. Testing must pass before the camera is added.`;
  let open= root.querySelector("#discovery-device-panel [data-discovery-open-existing]");
  if(linked&&!open){open=document.createElement("button");open.type="button";open.className="discovery-open-camera";open.dataset.discoveryOpenExisting="";open.textContent="Open existing camera";candidateStatus.after(open);}
  if(linked&&open)open.onclick=()=>void inspectCameraFromStatus(linked.id);
  else open?.remove();
  const unavailable=!current||Boolean(current.linked_camera_id)||Boolean(current.add_in_progress);
  for(const button of root.querySelectorAll("#discovery-device-panel [data-discovery-submit]"))button.disabled=unavailable;
}

function discoveryField(labelText,control) {
  const label=document.createElement("label");label.textContent=labelText;label.append(control);return label;
}

function buildDiscoveryCandidateSetup(candidate) {
  const setup=document.createElement("fieldset");setup.className="discovery-candidate-setup";setup.dataset.configSection="discovery-candidate-setup";
  const legend=document.createElement("legend");legend.textContent="Test and add this camera";setup.append(legend);
  const name=document.createElement("input");name.maxLength=120;name.required=true;name.autocomplete="off";name.value=window.NvrCameraDiscovery.candidateLabel(candidate);name.dataset.discoveryField="name";
  setup.append(discoveryField("Camera name",name));
  const address=document.createElement("input");address.inputMode="decimal";address.autocomplete="off";address.required=true;address.value=window.NvrCameraDiscovery.candidateAddress(candidate);address.dataset.discoveryField="address";
  setup.append(discoveryField("Camera IPv4 address",address));
  const serviceUrl=(candidate.service_urls||[]).map(safeDiscoveredEndpoint).find(Boolean)||"";
  const port=document.createElement("input");port.type="number";port.min="1";port.max="65535";port.value=(()=>{try{return new URL(serviceUrl).port|| (new URL(serviceUrl).protocol==="https:"?"443":"80");}catch{return "80";}})();port.dataset.discoveryField="port";
  setup.append(discoveryField("ONVIF port",port));
  const brand=document.createElement("select");brand.dataset.discoveryField="brand";
  for(const [value,label] of [["auto","Auto-detect / ONVIF"],["acti","ACTi"],["axis","Axis"],["bosch","Bosch"],["dahua","Dahua"],["hanwha","Hanwha"],["hikvision","Hikvision"],["reolink","Reolink"],["samsung","Samsung"],["uniview","Uniview"],["other","Other / generic ONVIF"]])brand.append(new Option(label,value));
  setup.append(discoveryField("Camera brand",brand));
  const username=document.createElement("input");username.autocomplete="off";username.dataset.discoveryField="username";
  setup.append(discoveryField("Camera username (optional)",username));
  const password=document.createElement("input");password.type="password";password.autocomplete="new-password";password.dataset.discoveryField="password";
  setup.append(discoveryField("Camera password (optional)",password));
  const confirm=document.createElement("input");confirm.type="password";confirm.autocomplete="new-password";confirm.dataset.discoveryField="password-confirm";
  setup.append(discoveryField("Confirm camera password",confirm));
  const endpoint=document.createElement("input");endpoint.type="url";endpoint.autocomplete="off";endpoint.value=serviceUrl;endpoint.dataset.discoveryField="endpoint";
  setup.append(discoveryField("ONVIF device endpoint URL",endpoint));
  const profiles=document.createElement("select");profiles.dataset.discoveryField="profile";profiles.append(new Option("Load stream profiles first",""));
  const substream=document.createElement("select");substream.dataset.discoveryField="substream";substream.append(new Option("Use main stream for live view",""));
  const loadProfiles=document.createElement("button");loadProfiles.type="button";loadProfiles.textContent="Load stream profiles";
  const profileMessage=document.createElement("p");profileMessage.className="discovery-profile-message";profileMessage.setAttribute("role","status");
  endpoint.addEventListener("input",()=>{profiles.replaceChildren(new Option("Load stream profiles first",""));substream.replaceChildren(new Option("Use main stream for live view",""));});
  loadProfiles.addEventListener("click",async()=>{
    const selectionGeneration=discoverySelectionGeneration;
    loadProfiles.disabled=true;profileMessage.textContent="Loading advertised stream profiles…";
    try {
      if(password.value!==confirm.value)throw new Error("Camera passwords do not match");
      const portNumber=Number(port.value);if(!Number.isInteger(portNumber)||portNumber<1||portNumber>65535)throw new Error("Enter a valid camera ONVIF port");
      const device_service_url=discoveryEndpointUrl(endpoint.value.trim()||`http://${address.value.trim()}:${portNumber}/onvif/device_service`);
      const result=await api("/api/cameras/onvif-profiles",{method:"POST",body:JSON.stringify({camera_ip:address.value.trim(),camera_username:username.value||null,camera_password:password.value||null,camera_brand:brand.value,device_service_url})});
      if(selectionGeneration!==discoverySelectionGeneration||state.selectedCandidateId!==candidate.id)return;
      endpoint.value=discoveryEndpointUrl(result.device_service_url||device_service_url);
      profiles.replaceChildren(...result.profiles.map(profile=>new Option(profile.name,profile.token)));
      if(result.profiles.length>1)profiles.prepend(new Option("Select a main stream",""));
      substream.replaceChildren(new Option("Use main stream for live view",""),...result.profiles.map(profile=>new Option(profile.name,profile.token)));
      profileMessage.textContent=result.profiles.length?`Found ${result.profiles.length} stream profile(s).`:"The camera did not advertise a stream profile.";
    } catch(error) {
      if(selectionGeneration===discoverySelectionGeneration)profileMessage.textContent=safeDiscoveryError(error,[username.value,password.value,confirm.value]);
    } finally {loadProfiles.disabled=false;}
  });
  setup.append(loadProfiles,discoveryField("Main stream profile",profiles),discoveryField("Live substream profile (optional)",substream),profileMessage);
  const add=document.createElement("button");add.type="button";add.className="primary";add.textContent="Test connection and add camera";add.dataset.discoverySubmit="";
  add.addEventListener("click",async()=>{
    const selectionGeneration=discoverySelectionGeneration;
    const current=state.candidates.find(item=>item.id===candidate.id);
    if(!current||current.linked_camera_id||current.add_in_progress) {updateDiscoverySelectedStatus(candidate);return;}
    if(discoveryAddPendingIds.has(candidate.id))return;
    const sensitive=[username.value,password.value,confirm.value];
    let submitted=false;
    try {
      if(!name.value.trim())throw new Error("Enter a camera name");
      if(password.value!==confirm.value)throw new Error("Camera passwords do not match");
      if(!profiles.value)throw new Error("Load stream profiles and select a main stream first");
      const portNumber=Number(port.value);if(!Number.isInteger(portNumber)||portNumber<1||portNumber>65535)throw new Error("Enter a valid camera ONVIF port");
      const device_service_url=discoveryEndpointUrl(endpoint.value.trim()||`http://${address.value.trim()}:${portNumber}/onvif/device_service`);
      discoveryAddPendingIds.add(candidate.id);add.disabled=true;add.textContent="Testing camera connection…";
      profileMessage.textContent="The stream is being tested. Recording will remain disabled.";
      submitted=true;
      const camera=await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/add`,{method:"POST",body:JSON.stringify({name:name.value.trim(),camera_ip:address.value.trim(),camera_username:username.value||null,camera_password:password.value||null,camera_brand:brand.value,onvif_profile_token:profiles.value,live_substream_profile_token:substream.value||null,device_service_url,motion_username:username.value||null,motion_password:password.value||null,recording_mode:"motion",recording_enabled:false})});
      if(selectionGeneration===discoverySelectionGeneration&&state.selectedCandidateId===candidate.id){clearConfigDraft(setup);state.selectedCandidateId=null;discoverySelectedSnapshot=null;discoveryRenderedCandidateId=null;}
      discoveryStatusMessage(`${camera.name} was added after its connection test. Recording is disabled; review its recording settings before enabling it.`);
      await refreshApp();
      const statusMessage=root.querySelector("#discovery-message");if(statusMessage){statusMessage.tabIndex=-1;statusMessage.focus();}
    } catch(error) {
      if(selectionGeneration===discoverySelectionGeneration)profileMessage.textContent=safeDiscoveryError(error,sensitive);
      if(submitted)await refreshDiscovery();
    } finally {discoveryAddPendingIds.delete(candidate.id);add.disabled=false;add.textContent="Test connection and add camera";}
  });
  setup.append(add);
  if(state.cameras.length){
    const linkFieldset=document.createElement("fieldset");linkFieldset.className="discovery-link-existing";linkFieldset.dataset.configSection="discovery-candidate-setup";
    const linkLegend=document.createElement("legend");linkLegend.textContent="Link to an existing camera";linkFieldset.append(linkLegend);
    const existing=document.createElement("select");existing.dataset.discoveryField="existing-camera";
    for(const camera of state.cameras){const option=new Option(`${camera.name}${camera.archived_at?" · Archived":""}`,camera.id);existing.append(option);}
    linkFieldset.append(discoveryField("Existing camera",existing));
    const link=document.createElement("button");link.type="button";link.textContent="Link identity to selected camera";link.dataset.discoverySubmit="";
    link.addEventListener("click",async()=>{
      const selectionGeneration=discoverySelectionGeneration;
      const current=state.candidates.find(item=>item.id===candidate.id);
      if(!current||current.linked_camera_id||current.add_in_progress){updateDiscoverySelectedStatus(candidate);return;}
      if(!guardDraftsIn(root.querySelector("#discovery-device-panel")))return;
      link.disabled=true;link.textContent="Linking device identity…";
      try {
        await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/link`,{method:"POST",body:JSON.stringify({camera_id:existing.value})});
        clearConfigDraft(linkFieldset);
        if(selectionGeneration===discoverySelectionGeneration)discoveryStatusMessage("Discovered identity linked to the existing camera. Camera ID and recording history were preserved.");
        await refreshApp();
        requestAnimationFrame(()=>root.querySelector("#discovery-device-panel [data-discovery-open-existing]")?.focus());
      } catch(error) {if(selectionGeneration===discoverySelectionGeneration)profileMessage.textContent=safeDiscoveryError(error);await refreshDiscovery();}
      finally {link.disabled=false;link.textContent="Link identity to selected camera";}
    });
    linkFieldset.append(link);setup.append(linkFieldset);
  }
  return setup;
}

async function selectDiscoveryCandidate(candidateId) {
  if(candidateId===state.selectedCandidateId)return;
  const panel=root.querySelector("#discovery-device-panel");
  if(panel&&!guardDraftsIn(panel)){renderDiscoveryResults();requestAnimationFrame(()=>root.querySelector(`#discovery-table-scroll [data-discovery-candidate-id="${CSS.escape(candidateId)}"]`)?.focus());return;}
  if(!window.NvrCameraDiscovery.selectCandidate(state,candidateId))return;
  discoverySelectionGeneration+=1;discoverySelectedSnapshot=state.candidates.find(candidate=>candidate.id===candidateId)||null;
  discoveryRenderedCandidateId=null;renderDiscoveryResults();renderDiscoverySelection();
  requestAnimationFrame(()=>root.querySelector(`#discovery-table-scroll [data-discovery-candidate-id="${CSS.escape(candidateId)}"]`)?.focus());
}

async function runDiscoveryScan(ip_range=null,requestedTimeout=null) {
  if(discoveryScanPending)return;
  const rangeInput=root.querySelector("#discovery-ip-range"),timeoutInput=root.querySelector("#discovery-timeout");
  const vlanButton=root.querySelector("#discover-cameras"),rangeButton=root.querySelector("#discover-ip-range"),retryButton=root.querySelector("#discovery-retry-scan");
  const range=ip_range===null?null:String(ip_range).trim();
  if(ip_range!==null&&!range){rangeInput?.focus();discoveryStatusMessage("Enter a private IPv4 CIDR range, such as 192.168.1.0/24.");return;}
  const timeoutSeconds=requestedTimeout??Number(timeoutInput?.value||5);
  discoveryLastScanRequest={ip_range:range,timeout_seconds:timeoutSeconds};
  discoveryScanPending=true;window.NvrCameraDiscovery.beginRefresh(state);
  for(const button of [vlanButton,rangeButton,retryButton])if(button)button.disabled=true;
  if(vlanButton)vlanButton.textContent=range?"Scan camera VLAN":"Scanning camera VLAN…";
  if(rangeButton)rangeButton.textContent=range?"Scanning private range…":"Scan private range";
  discoveryStatusMessage(range?`Scanning ${range} with bounded unicast ONVIF probes…`:"Scanning the camera VLAN for ONVIF devices…");
  try {
    const body={timeout_seconds:timeoutSeconds};if(range)body.ip_range=range;
    const result=await api("/api/discovery/scan",{method:"POST",body:JSON.stringify(body)});
    const when=formatDateTime(new Date());
    const targetText=range?`${result.targets_scanned} address(es) checked`:"camera VLAN scanned";
    discoveryLastScanText=`Last scan · ${range||"camera VLAN"} · ${targetText} · ${result.discovered_count} device(s) found · ${when}`;
    const lastScan=root.querySelector("#discovery-last-scan");if(lastScan)lastScan.textContent=discoveryLastScanText;
    const partial=(result.partial_failures||[]).join(" ");
    discoveryStatusMessage(`Scan complete: ${result.discovered_count} device(s) found. ${partial}`.trim());
    if(retryButton)retryButton.classList.add("hidden");
    await refreshDiscovery();
  } catch(error) {
    discoveryStatusMessage(`Scan failed. ${safeDiscoveryError(error)} The scan controls are ready to retry.`);
    if(retryButton){retryButton.classList.remove("hidden");retryButton.disabled=false;}
  } finally {
    discoveryScanPending=false;
    if(vlanButton){vlanButton.disabled=false;vlanButton.textContent="Scan camera VLAN";}
    if(rangeButton){rangeButton.disabled=false;rangeButton.textContent="Scan private range";}
    if(retryButton)retryButton.disabled=false;
  }
}

async function refreshDiscovery() {
  ensureDiscoveryWorkspace();
  const generation=window.NvrCameraDiscovery.beginRefresh(state);
  try {
    const [{candidates},inventory]=await Promise.all([api("/api/discovery/candidates"),api("/api/cameras")]);
    if(!window.NvrCameraDiscovery.isCurrentRefresh(state,generation))return;
    const previousId=state.selectedCandidateId;
    const previousCandidate=state.candidates.find(candidate=>candidate.id===previousId)||discoverySelectedSnapshot;
    const selectionMissing=previousId&&!candidates.some(candidate=>candidate.id===previousId);
    const panel=root.querySelector("#discovery-device-panel");
    const updatedCandidate=candidates.find(candidate=>candidate.id===previousId);
    const statusChanged=previousCandidate&&updatedCandidate&&(previousCandidate.linked_camera_id!==updatedCandidate.linked_camera_id||previousCandidate.add_in_progress!==updatedCandidate.add_in_progress);
    if(selectionMissing&&panel?.querySelector('[data-config-dirty="true"]')&&!guardDraftsIn(panel)){
      state.candidates=[...candidates];state.cameras=[...inventory.cameras];discoverySelectedSnapshot=previousCandidate;
      renderDiscoveryResults();renderDiscoverySelection();return;
    }
    window.NvrCameraDiscovery.replaceResults(state,candidates,inventory.cameras);
    discoveryResultsLoaded=true;
    if(statusChanged&&!panel?.querySelector('[data-config-dirty="true"]'))discoveryRenderedCandidateId=null;
    discoverySelectedSnapshot=state.candidates.find(candidate=>candidate.id===state.selectedCandidateId)||null;
    renderDiscoveryResults();renderDiscoverySelection();
  } catch(error) {
    if(!window.NvrCameraDiscovery.isCurrentRefresh(state,generation))return;
    discoveryStatusMessage(`Could not refresh discovered devices. ${safeDiscoveryError(error)}`);
    if(!discoveryResultsLoaded){
      const count=root.querySelector("[data-discovery-count]"),scroll=root.querySelector("#discovery-table-scroll");
      if(count)count.textContent="Discovery results unavailable";
      if(scroll){const unavailable=document.createElement("p");unavailable.className="discovery-empty-state";unavailable.textContent="The candidate list could not be loaded. Use Refresh results to retry.";scroll.replaceChildren(unavailable);}
    }
  }
}
    ensureDiscoveryWorkspace();
    root.querySelector("#discover-cameras")?.addEventListener("click", () => void runDiscoveryScan());
    root.querySelector("#discover-ip-range")?.addEventListener("click", () => void runDiscoveryScan(root.querySelector("#discovery-ip-range")?.value.trim() || null));
    return { refresh: refreshDiscovery, state };
  }

  return {
    mountWorkspace,
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
