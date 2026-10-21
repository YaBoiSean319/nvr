const sections = ["setup", "login", "status"];
const message = document.querySelector("#message");
let currentRole = null;
let importCanApply = false;
let lastPreviewCsv = "";
let liveViews = [];
let liveStatusTimer = null;
const LIVE_GRID_SIZES = [1, 4, 9, 16];
let activeLiveCameraIds = [];
let activeLiveTileCameraIds = [];
let activeLiveGridSize = 4;
let focusedLiveCameraId = null;
let latestCameras = [];
let playbackTimeline = null;
let playbackViews = [];
let playbackMaster = null;
let playbackSyncGuard = false;
let playbackWindow = null;
let playbackManualChoices = new Map();
let playbackResultsStale = false;
let exportPollTimer = null;
let incidentPollTimer = null;
let webhookPollTimer = null;
let consoleShellReady = false;
let activeScreen = "live";
let sidebarCameras = [];
let sidebarGroups = [];
let sidebarViews = [];
let workspaceRestored = false;
let workspaceSaveTimer = null;
let liveViewDirty = false;
let cachedLiveArrangement = null;
let displaySettings = {time_zone:"UTC",date_format:"friendly",clock_format:"12"};
function formatDateTime(value, compact=false, settings=displaySettings) {
  const date=new Date(value);
  const common={timeZone:settings.time_zone};
  const parts=Object.fromEntries(new Intl.DateTimeFormat("en-US",{...common,year:"numeric",month:settings.date_format==="long"?"long":settings.date_format==="iso"||settings.date_format==="day-month"||settings.date_format==="month-day"?"2-digit":"short",day:"2-digit"}).formatToParts(date).map(part=>[part.type,part.value]));
  let dateText;
  if(compact) dateText=`${parts.month}/${parts.day}`;
  else if(settings.date_format==="iso") dateText=`${parts.year}-${parts.month}-${parts.day}`;
  else if(settings.date_format==="day-month") dateText=`${parts.day}/${parts.month}/${parts.year}`;
  else if(settings.date_format==="month-day") dateText=`${parts.month}/${parts.day}/${parts.year}`;
  else dateText=`${parts.month} ${Number(parts.day)}, ${parts.year}`;
  const timeOptions={...common,hour:"2-digit",minute:"2-digit",hourCycle:settings.clock_format==="24"?"h23":"h12"};
  if(!compact)timeOptions.second="2-digit";
  const time=new Intl.DateTimeFormat("en-US",timeOptions).format(date);
  return `${dateText} ${time}`;
}
function localTimeCandidates(value) {
  const match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value||"");
  if(!match)return [];
  const expected=match.slice(1).map(Number),target=Date.UTC(expected[0],expected[1]-1,expected[2],expected[3],expected[4]);
  const formatter=new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn",{timeZone:displaySettings.time_zone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"});
  const offsets=new Set();
  for(let hour=-36;hour<=36;hour+=6){const probe=target+hour*3600000;const p=Object.fromEntries(formatter.formatToParts(new Date(probe)).map(item=>[item.type,item.value]));offsets.add(Date.UTC(Number(p.year),Number(p.month)-1,Number(p.day),Number(p.hour),Number(p.minute))-probe);}
  return [...new Set([...offsets].map(offset=>target-offset).filter(ms=>{const p=Object.fromEntries(formatter.formatToParts(new Date(ms)).map(item=>[item.type,item.value]));return Number(p.year)===expected[0]&&Number(p.month)===expected[1]&&Number(p.day)===expected[2]&&Number(p.hour)===expected[3]&&Number(p.minute)===expected[4];}))].sort((a,b)=>a-b);
}
function updateFoldChoices(field) {
  const input=document.querySelector(`#playback-${field}`),select=document.querySelector(`#playback-${field}-fold`),label=document.querySelector(`#playback-${field}-fold-label`),candidates=localTimeCandidates(input.value);
  select.replaceChildren(new Option("Choose occurrence",""));
  if(candidates.length>1){for(const [index,instant] of candidates.entries()){const zoneOffset=new Intl.DateTimeFormat("en-US",{timeZone:displaySettings.time_zone,timeZoneName:"shortOffset"}).formatToParts(new Date(instant)).find(part=>part.type==="timeZoneName")?.value||`Occurrence ${index+1}`;select.add(new Option(`${index===0?"First":"Second"} occurrence · ${zoneOffset}`,String(instant)));}select.classList.remove("hidden");label.classList.remove("hidden");}
  else{select.classList.add("hidden");label.classList.add("hidden");}
}
function localInputForInstant(instant) {
  const parts=Object.fromEntries(new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn",{timeZone:displaySettings.time_zone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(instant).map(part=>[part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}
function initializePlaybackDefaultRange() {
  const start=document.querySelector("#playback-start"),end=document.querySelector("#playback-end");
  if(!start||!end||start.value||end.value)return;
  const now=new Date();start.value=localInputForInstant(new Date(now.getTime()-3600000));end.value=localInputForInstant(now);
}
function resolveSearchLocalTime(field) {
  const input=document.querySelector(`#playback-${field}`),candidates=localTimeCandidates(input.value),fold=document.querySelector(`#playback-${field}-fold`);
  if(!input.value)throw new Error("Enter both a start and end time.");
  if(candidates.length===0)throw new Error(`The ${field} time does not exist in ${displaySettings.time_zone} because the clock jumps forward.`);
  if(candidates.length>1){const chosen=Number(fold.value);if(!candidates.includes(chosen))throw new Error(`Choose which occurrence of the repeated ${field} time to use.`);return new Date(chosen).toISOString();}
  return new Date(candidates[0]).toISOString();
}
function show(name) { if(name!=="status") stopSynchronizedPlayback(); document.querySelector("main > h1").classList.toggle("hidden", name === "status"); for (const id of sections) document.querySelector(`#${id}`).classList.toggle("hidden", id !== name); }
function moveInto(selector, targetSelector) {
  const element = document.querySelector(selector), target = document.querySelector(targetSelector);
  if (element && target) target.append(element);
}
function setupConsoleShell() {
  if (consoleShellReady) return;
  const live = document.querySelector("#live-page-content"), footage = document.querySelector("#footage-page-content");
  const cameraSettings = document.querySelector("#camera-settings-content"), appSettings = document.querySelector("#app-settings-content");
  for (const selector of ["#live-heading", "#incidents-heading"]) {
    const heading = document.querySelector(selector);
    if (heading) live.append(heading.closest("section"));
  }
  for (const selector of ["#playback-heading"]) {
    const heading = document.querySelector(selector);
    if (heading) footage.append(heading.closest("section"));
  }
  for (const selector of ["#camera-heading", "#discovery-admin", "#bulk-admin", "#camera-form", "#camera-message", "#camera-list", "#segment-list", "#playback"]) {
    if (selector === "#camera-heading") {
      const heading = document.querySelector(selector);
      if (heading) cameraSettings.append(heading);
    } else moveInto(selector, "#camera-settings-content");
  }
  const recordedHeading = [...document.querySelectorAll("h3")].find(item => item.textContent.trim() === "Recorded footage");
  if (recordedHeading) cameraSettings.append(recordedHeading);
  moveInto("#status > .badge", "#app-settings-content");
  for (const selector of ["#status-heading", "#status-list", "#session-admin", "#diagnostics-admin", "#webhooks-admin", "#heartbeat-admin", "#display-settings-admin", "#logout"]) moveInto(selector, "#app-settings-content");
  document.querySelectorAll("[data-screen]").forEach(button => button.addEventListener("click", () => setScreen(button.dataset.screen)));
  document.querySelector("#sidebar-toggle").addEventListener("click", () => {
    const layout = document.querySelector("#console-layout"), button = document.querySelector("#sidebar-toggle");
    const collapsed = layout.classList.toggle("sidebar-collapsed");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "Expand camera sidebar" : "Collapse camera sidebar");
    saveLiveWorkspace();
  });
  document.querySelector("#sidebar-search").addEventListener("input", filterSidebar);
  const theme = document.querySelector("#theme-choice"), savedTheme = localStorage.getItem("nvr-theme") || "system";
  theme.value = savedTheme; document.documentElement.dataset.theme = savedTheme === "system" ? "" : savedTheme;
  theme.addEventListener("change", () => { localStorage.setItem("nvr-theme", theme.value); document.documentElement.dataset.theme = theme.value === "system" ? "" : theme.value; });
  for(const selector of ["#display-time-zone","#display-date-format","#display-clock-format"])document.querySelector(selector).addEventListener("input",previewDisplaySettings);
  document.querySelector("#display-settings-save").addEventListener("click",saveDisplaySettings);
  for(const field of ["start","end"])document.querySelector(`#playback-${field}`).addEventListener("change",()=>updateFoldChoices(field));
  for(const field of ["start","end"])document.querySelector(`#playback-${field}-fold`).addEventListener("change",markPlaybackResultsStale);
  for(const selector of ["#playback-start","#playback-end"])document.querySelector(selector).addEventListener("input",markPlaybackResultsStale);
  document.querySelector("#playback-camera-picker").addEventListener("change",event=>{
    if(event.target.matches("[data-playback-camera]")){
      playbackManualChoices.set(event.target.value,event.target.checked);
      syncPlaybackGroupChecks();markPlaybackResultsStale();
    } else if(event.target.matches("[data-playback-group]")){
      const group=sidebarGroups.find(item=>item.id===event.target.value),members=new Set(group?.camera_ids||[]);
      for(const cameraId of members)playbackManualChoices.delete(cameraId);
      syncPlaybackCameraChecks();markPlaybackResultsStale();
    }
  });
  document.querySelector("#playback-grid-size").addEventListener("change",markPlaybackResultsStale);
  document.querySelector("#playback-zoom-in").addEventListener("click",()=>zoomPlaybackTimeline(.5));
  document.querySelector("#playback-zoom-out").addEventListener("click",()=>zoomPlaybackTimeline(2));
  document.querySelector("#playback-fit-range").addEventListener("click",fitPlaybackTimeline);
  setupPlaybackRulerGestures();
  consoleShellReady = true;
}
function readDisplaySettingsForm() {
  return {time_zone:document.querySelector("#display-time-zone").value.trim(),date_format:document.querySelector("#display-date-format").value,clock_format:document.querySelector("#display-clock-format").value};
}
function updateSiteTimeContext() {
  const target=document.querySelector("#site-time-context");
  if(target)target.textContent=`Search times use ${displaySettings.time_zone}. Repeated fall-back times require choosing an occurrence; nonexistent spring-forward times are rejected.`;
}
async function previewDisplaySettings() {
  const target=document.querySelector("#display-settings-preview"),draft=readDisplaySettingsForm();
  try {
    const result=await api("/api/settings/display/preview",{method:"POST",body:JSON.stringify(draft)});
    target.textContent=`Preview: ${result.preview} · ${draft.time_zone}`;
    document.querySelector("#display-settings-message").textContent="Preview only; save to apply these settings.";
  } catch(error) { target.textContent="Preview unavailable until the time zone and display options are valid.";document.querySelector("#display-settings-message").textContent=error.message; }
}
async function saveDisplaySettings() {
  const message=document.querySelector("#display-settings-message"),button=document.querySelector("#display-settings-save");
  button.disabled=true;
  try {
    displaySettings=await api("/api/settings/display",{method:"PUT",body:JSON.stringify(readDisplaySettingsForm())});
    updateSiteTimeContext();
    for(const field of ["start","end"])updateFoldChoices(field);
    markPlaybackResultsStale();
    message.textContent="Site date and time display settings saved.";
    await previewDisplaySettings();
  } catch(error) { message.textContent=error.message; }
  finally { button.disabled=false; }
}
async function refreshDisplaySettings() {
  displaySettings=await api("/api/settings/display");
  document.querySelector("#display-time-zone").value=displaySettings.time_zone;
  document.querySelector("#display-date-format").value=displaySettings.date_format;
  document.querySelector("#display-clock-format").value=displaySettings.clock_format;
  updateSiteTimeContext();
  initializePlaybackDefaultRange();
  for(const field of ["start","end"])updateFoldChoices(field);
  if(currentRole==="administrator")await previewDisplaySettings();
}
function setScreen(screen) {
  if (!document.querySelector(`[data-page="${screen}"]`)) return;
  if (activeScreen === "live" && screen !== "live") {
    if(activeLiveCameraIds.length)cachedLiveArrangement={camera_ids:[...activeLiveCameraIds],tile_camera_ids:[...activeLiveTileCameraIds],grid_size:activeLiveGridSize};
    stopLiveViews();
  }
  if (screen !== "footage") stopSynchronizedPlayback();
  activeScreen = screen;
  document.querySelectorAll("[data-page]").forEach(page => page.classList.toggle("hidden", page.dataset.page !== screen));
  document.querySelectorAll("#console-nav [data-screen]").forEach(button => {
    if (button.classList.contains("hidden")) return;
    if (button.dataset.screen === screen) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if(screen==="live"&&cachedLiveArrangement&&!activeLiveCameraIds.length){
    const prior=cachedLiveArrangement;cachedLiveArrangement=null;
    for(const input of document.querySelectorAll("[data-live-camera]"))input.checked=prior.camera_ids.includes(input.value);
    startLiveViews(prior.camera_ids,latestCameras,null,prior.grid_size,prior.tile_camera_ids);
    clearLiveViewDirty();
  }
}
function filterSidebar() {
  const query = document.querySelector("#sidebar-search").value.trim().toLocaleLowerCase();
  document.querySelectorAll("#console-sidebar li").forEach(item => { item.hidden = !item.dataset.searchText.includes(query); });
}
function sidebarItem(target, label, searchText, activate, health = null) {
  const row = document.createElement("li"); row.dataset.searchText = searchText.toLocaleLowerCase();
  const button = document.createElement("button"); button.type = "button"; button.append(document.createTextNode(label));
  if (health) { const dot = document.createElement("span"); dot.className = "health-dot"; dot.dataset.health = health; dot.setAttribute("aria-label", `Health: ${health.replaceAll("_", " ")}`); button.append(dot); }
  button.addEventListener("click", activate); row.append(button); target.append(row); return row;
}
function renderSidebar() {
  const cameraList = document.querySelector("#sidebar-cameras"), groupList = document.querySelector("#sidebar-groups"), viewList = document.querySelector("#sidebar-views");
  cameraList.replaceChildren(); groupList.replaceChildren(); viewList.replaceChildren();
  for (const camera of [...sidebarCameras].sort((a, b) => a.name.localeCompare(b.name))) {
    let address = "";
    try { address = new URL(camera.stream_url).hostname; } catch {}
    const row=sidebarItem(cameraList, camera.name, `${camera.name} ${address}`, () => {
    setScreen("live"); const choice = [...document.querySelectorAll("[data-live-camera]")].find(input => input.value === camera.id);
    if (choice) choice.checked = true;
    }, camera.recording_health?.state || camera.recording_status || "unknown");
    row.draggable=true;
    row.addEventListener("dragstart",event=>event.dataTransfer.setData("text/plain",camera.id));
    row.addEventListener("dblclick",()=>{setScreen("live");const choice=[...document.querySelectorAll("[data-live-camera]")].find(input=>input.value===camera.id);if(choice)choice.checked=true;addCameraToLiveGrid(camera.id,latestCameras);});
  }
  for (const group of sidebarGroups) sidebarItem(groupList, group.name, group.name, () => {
    setScreen("live"); const members = new Set(group.camera_ids || []);
    document.querySelectorAll("[data-live-camera]").forEach(input => { input.checked = members.has(input.value); });
    if (!members.size) document.querySelector("#live-message").textContent = "This group has no available cameras.";
  });
  for (const view of sidebarViews) sidebarItem(viewList, `${view.name} · ${view.scope}`, `${view.name} ${view.scope}`, () => applySavedView(view));
  filterSidebar();
}
async function showLogin() {
  const state = await api("/api/setup/status");
  show(state.setup_required ? "setup" : "login");
  if (!state.setup_required) {
    try { document.querySelector("#entra-login").classList.toggle("hidden", !(await api("/api/auth/oidc/status")).enabled); }
    catch { document.querySelector("#entra-login").classList.add("hidden"); }
  }
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers }, credentials: "same-origin" });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}
function cameraSourcePayload() {
  const password=document.querySelector("#camera-password").value;
  if(password!==document.querySelector("#camera-password-confirm").value) throw new Error("Camera passwords do not match");
  const profile=document.querySelector("#camera-profile").value;
  if(!profile) throw new Error("Load camera endpoints and select a stream profile first");
  return {camera_ip:document.querySelector("#camera-ip").value.trim(),camera_username:document.querySelector("#camera-username").value||null,camera_password:password||null,camera_brand:document.querySelector("#camera-brand").value,onvif_profile_token:profile,live_substream_profile_token:document.querySelector("#camera-live-profile").value||null,device_service_url:document.querySelector("#motion-service-url").value||null};
}
async function loadCameraProfiles() {
  const address=document.querySelector("#camera-ip").value.trim();
  const port=Number(document.querySelector("#camera-port").value);
  if(!address || !Number.isInteger(port) || port<1 || port>65535) throw new Error("Enter a camera IPv4 address and a valid ONVIF port");
  const password=document.querySelector("#camera-password").value;
  if(password!==document.querySelector("#camera-password-confirm").value) throw new Error("Camera passwords do not match");
  const device_service_url=document.querySelector("#motion-service-url").value.trim()||`http://${address}:${port}/onvif/device_service`;
  const result=await api("/api/cameras/onvif-profiles",{method:"POST",body:JSON.stringify({camera_ip:address,camera_username:document.querySelector("#camera-username").value||null,camera_password:password||null,camera_brand:document.querySelector("#camera-brand").value,device_service_url})});
  const select=document.querySelector("#camera-profile");
  select.replaceChildren(...result.profiles.map(profile=>new Option(profile.name,profile.token)));
  if(result.profiles.length>1) select.prepend(new Option("Select stream profile",""));
  const liveProfile=document.querySelector("#camera-live-profile");
  liveProfile.replaceChildren(new Option("Use main stream (scaled for grid)",""),...result.profiles.map(profile=>new Option(profile.name,profile.token)));
  document.querySelector("#motion-service-url").value=result.device_service_url;
  document.querySelector("#motion-username").value=document.querySelector("#camera-username").value;
  document.querySelector("#motion-password").value=password;
  document.querySelector("#camera-profile-message").textContent=`Found ${result.profiles.length} ONVIF stream profile(s).`;
}
async function refresh() {
  message.textContent = "";
  try {
    const who = await api("/api/auth/me");
    currentRole = who.role;
    const admin = currentRole === "administrator";
    document.querySelector("#live-layout-scope option[value=shared]").classList.toggle("hidden", !admin);
    setupConsoleShell();
    document.querySelectorAll('#console-nav [data-screen="camera-settings"], #console-nav [data-screen="app-settings"]').forEach(button => button.classList.toggle("hidden", !admin));
    if (!admin && ["camera-settings", "app-settings"].includes(activeScreen)) setScreen("live");
    document.querySelector("#status-heading").textContent = admin ? `System status · ${who.username}` : `Cameras · ${who.username}`;
    document.querySelector("#status-list").classList.toggle("hidden", !admin);
    document.querySelector("#display-settings-admin").classList.toggle("hidden", !admin);
    document.querySelector("#session-admin").classList.toggle("hidden", !admin);
    document.querySelector("#diagnostics-admin").classList.toggle("hidden", !admin);
    document.querySelector("#webhooks-admin").classList.toggle("hidden", !admin);
    document.querySelector("#heartbeat-admin").classList.toggle("hidden", !admin);
    document.querySelector("#camera-form").classList.toggle("hidden", !admin);
    document.querySelector("#discovery-admin").classList.toggle("hidden", !admin);
    document.querySelector("#bulk-admin").classList.toggle("hidden", !admin);
    await refreshDisplaySettings();
    if (admin) {
      const data = await api("/api/system/status");
      const rows = [["Version", data.version], ["Database", data.database], ["Administrators", data.configured_administrators], ["Storage available", data.storage.available_bytes == null ? "Unavailable" : `${(data.storage.available_bytes / 1073741824).toFixed(1)} GiB`]];
      document.querySelector("#status-list").replaceChildren(...rows.flatMap(([k,v]) => { const dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=k; dd.textContent=v; return [dt,dd]; }));
      await refreshSessions();
      await refreshDiagnostics();
      await refreshWebhooks();
      await refreshHeartbeatStatus();
      await loadAiRuntimeSettings();
    }
    const inventory = await api("/api/cameras");
    latestCameras = inventory.cameras;
    sidebarCameras = inventory.cameras;
    const {groups} = await api("/api/camera-groups");
    sidebarGroups = groups;
    if (admin) await refreshGroups();
    await refreshIncidents();
    renderCameras(inventory.cameras);
    renderLiveCameraPicker(inventory.cameras);
    renderPlaybackCameraPicker(inventory.cameras,sidebarGroups);
    await refreshLiveLayouts();
    if (!workspaceRestored) { await restoreLiveWorkspace(); workspaceRestored = true; }
    renderSidebar();
    await refreshExports();
    if (admin) await refreshDiscovery();
    setScreen(activeScreen === "footage" ? "footage" : "live");
    show("status");
  } catch (error) {
    if (error.message === "Authentication required" || error.message === "Session expired") {
      currentRole = null; await showLogin();
    } else { message.textContent = error.message; }
  }
}
function renderLiveCameraPicker(cameras) {
  const target=document.querySelector("#live-camera-picker");
  target.replaceChildren(...cameras.map(camera=>{
    const label=document.createElement("label"); label.textContent=`${camera.name}${camera.live_substream_available?" · low-resolution profile":""} `;
    const input=document.createElement("input"); input.type="checkbox"; input.dataset.liveCamera="true"; input.value=camera.id;
    label.append(input); return label;
  }));
}
async function refreshLiveLayouts() {
  try {
    const {layouts,shared_views=[]}=await api("/api/live/views");
    sidebarViews = [...layouts,...shared_views];
    const select=document.querySelector("#live-layouts"), previous=select.value;
    select.replaceChildren(new Option("Choose a saved view",""),...sidebarViews.map(layout=>new Option(`${layout.name} · ${layout.scope}`,`${layout.scope}:${layout.name}`)));
    if(sidebarViews.some(layout=>`${layout.scope}:${layout.name}`===previous)) select.value=previous;
    renderSidebar();
  } catch(error) { document.querySelector("#live-message").textContent=error.message; }
}
async function refreshIncidents() {
  const target=document.querySelector("#incidents-list"), message=document.querySelector("#incidents-message");
  if(!currentRole) { target.replaceChildren(); return; }
  try {
    const result=await api("/api/incidents");
    message.textContent="";
    target.replaceChildren(...result.incidents.map(incident=>{
      const item=document.createElement("li");
      const summary=document.createElement("strong");
      summary.textContent=`${incident.state.toUpperCase()} · ${incident.summary}`;
      const details=document.createElement("p");
      details.textContent=`First seen ${formatDateTime(incident.first_seen_at)} · Last seen ${formatDateTime(incident.last_seen_at)} · Recurrences ${Math.max(0,incident.recurrence_count-1)} · ${incident.events.length} history events`;
      item.append(summary,details);
      if(incident.state==="active") {
        const acknowledge=document.createElement("button"); acknowledge.type="button"; acknowledge.textContent="Acknowledge";
        acknowledge.addEventListener("click",async()=>{
          acknowledge.disabled=true;
          try { await api(`/api/incidents/${encodeURIComponent(incident.id)}/acknowledge`,{method:"POST"}); await refreshIncidents(); }
          catch(error) { message.textContent=error.message; acknowledge.disabled=false; }
        });
        item.append(acknowledge);
      }
      return item;
    }));
    if(!result.incidents.length) { const item=document.createElement("li"); item.textContent="No incidents recorded."; target.append(item); }
  } catch(error) { message.textContent=error.message; }
}
async function refreshWebhooks() {
  const list=document.querySelector("#webhook-list"), deliveries=document.querySelector("#webhook-deliveries"), options=document.querySelector("#webhook-event-options");
  try {
    const [{webhooks},{deliveries:history},{event_types}]=await Promise.all([
      api("/api/webhooks"), api("/api/webhooks/deliveries?limit=50"), api("/api/webhooks/event-types")
    ]);
    if(!options.childElementCount) {
      options.replaceChildren(...event_types.filter(type=>type!=="webhook.test").map(type=>{
        const label=document.createElement("label"),input=document.createElement("input"); input.type="checkbox";input.value=type;input.dataset.webhookEvent="true";input.checked=true;
        label.append(input,document.createTextNode(` ${type}`));return label;
      }));
    }
    list.replaceChildren(...webhooks.map(webhook=>{
      const item=document.createElement("li"),summary=document.createElement("span");
      summary.textContent=`${webhook.enabled?"Enabled":"Disabled"} · ${webhook.name} · ${webhook.url_origin} · ${webhook.events.join(", ")}${webhook.allow_private_receiver?" · private IP authorized":""}`;item.append(summary);
      const test=document.createElement("button");test.type="button";test.textContent="Send test";test.addEventListener("click",async()=>{try{const result=await api(`/api/webhooks/${encodeURIComponent(webhook.id)}/test`,{method:"POST"});document.querySelector("#webhook-message").textContent=`Test event ${result.event_id} queued.`;await refreshWebhooks();}catch(error){document.querySelector("#webhook-message").textContent=error.message;}});item.append(test);
      const toggle=document.createElement("button");toggle.type="button";toggle.textContent=webhook.enabled?"Disable":"Enable";toggle.addEventListener("click",async()=>{try{await api(`/api/webhooks/${encodeURIComponent(webhook.id)}`,{method:"PATCH",body:JSON.stringify({enabled:!webhook.enabled})});await refreshWebhooks();}catch(error){document.querySelector("#webhook-message").textContent=error.message;}});item.append(toggle);
      const rotate=document.createElement("button");rotate.type="button";rotate.textContent="Rotate signing secret";rotate.addEventListener("click",async()=>{try{const result=await api(`/api/webhooks/${encodeURIComponent(webhook.id)}/rotate-secret`,{method:"POST"});document.querySelector("#webhook-message").textContent=`New signing secret (shown once): ${result.signing_secret}`;}catch(error){document.querySelector("#webhook-message").textContent=error.message;}});item.append(rotate);
      const remove=document.createElement("button");remove.type="button";remove.textContent="Delete";remove.addEventListener("click",async()=>{if(!window.confirm(`Delete webhook receiver “${webhook.name}”?`))return;try{await api(`/api/webhooks/${encodeURIComponent(webhook.id)}`,{method:"DELETE"});await refreshWebhooks();}catch(error){document.querySelector("#webhook-message").textContent=error.message;}});item.append(remove);
      return item;
    }));
    if(!webhooks.length){const item=document.createElement("li");item.textContent="No webhook receivers configured.";list.append(item);}
    deliveries.replaceChildren(...history.map(delivery=>{const item=document.createElement("li");item.textContent=`${delivery.endpoint_name} · ${delivery.event_type} · ${delivery.status} · attempts ${delivery.attempt_count}${delivery.response_status?` · HTTP ${delivery.response_status}`:""}${delivery.error_code?` · ${delivery.error_code}`:""} · ${delivery.event_id}`;return item;}));
    if(!history.length){const item=document.createElement("li");item.textContent="No deliveries yet.";deliveries.append(item);}
  } catch(error) { document.querySelector("#webhook-message").textContent=error.message; }
}
async function refreshHeartbeatStatus() {
  try {
    const state=await api("/api/heartbeat");
    const target=document.querySelector("#heartbeat-state");
    if(!state.configured) target.textContent="No external heartbeat receiver is configured.";
    else {
      const lastSuccess=state.last_success_at?formatDateTime(state.last_success_at):"none yet";
      target.textContent=`${state.enabled?"Enabled":"Disabled"} · ${state.url_origin} · every ${state.interval_seconds}s · last successful send ${lastSuccess}${state.last_error?` · latest attempt failed: ${state.last_error}`:""}. ${state.checker_note}`;
    }
  }catch(error){document.querySelector("#heartbeat-message").textContent=error.message;}
}
document.querySelector("#heartbeat-save").addEventListener("click",async()=>{
  const message=document.querySelector("#heartbeat-message");
  try {
    const result=await api("/api/heartbeat",{method:"PUT",body:JSON.stringify({target_url:document.querySelector("#heartbeat-url").value.trim(),interval_seconds:Number(document.querySelector("#heartbeat-interval").value),allow_private_receiver:document.querySelector("#heartbeat-allow-private").checked})});
    message.textContent=result.signing_secret?`Heartbeat configured. Save this signing secret at the external receiver; it is shown once: ${result.signing_secret}`:"Heartbeat configuration updated. The signing secret was retained.";
    await refreshHeartbeatStatus();
  }catch(error){message.textContent=error.message;}
});
document.querySelector("#heartbeat-test").addEventListener("click",async()=>{
  try{await api("/api/heartbeat/test",{method:"POST"});document.querySelector("#heartbeat-message").textContent="Test heartbeat queued.";setTimeout(refreshHeartbeatStatus,1500);}
  catch(error){document.querySelector("#heartbeat-message").textContent=error.message;}
});
document.querySelector("#heartbeat-rotate").addEventListener("click",async()=>{
  try{const result=await api("/api/heartbeat/rotate-secret",{method:"POST"});document.querySelector("#heartbeat-message").textContent=`New signing secret (shown once): ${result.signing_secret}`;}
  catch(error){document.querySelector("#heartbeat-message").textContent=error.message;}
});
document.querySelector("#heartbeat-disable").addEventListener("click",async()=>{
  try{await api("/api/heartbeat",{method:"DELETE"});document.querySelector("#heartbeat-message").textContent="Heartbeat sender disabled. The independent receiver will alert when its timeout expires.";await refreshHeartbeatStatus();}
  catch(error){document.querySelector("#heartbeat-message").textContent=error.message;}
});
document.querySelector("#webhook-create").addEventListener("click",async()=>{
  const message=document.querySelector("#webhook-message");
  try {
    const name=document.querySelector("#webhook-name").value.trim(),url=document.querySelector("#webhook-url").value.trim();
    const events=[...document.querySelectorAll("[data-webhook-event]:checked")].map(input=>input.value);
    const result=await api("/api/webhooks",{method:"POST",body:JSON.stringify({name,url,events,allow_private_receiver:document.querySelector("#webhook-allow-private").checked})});
    message.textContent=`Receiver added. Save this signing secret now; it will not be shown again: ${result.signing_secret}`;
    document.querySelector("#webhook-name").value="";document.querySelector("#webhook-url").value="";document.querySelector("#webhook-allow-private").checked=false;
    await refreshWebhooks();
  } catch(error) { message.textContent=error.message; }
});
function stopLiveViews() {
  if(liveStatusTimer) clearInterval(liveStatusTimer);
  liveStatusTimer=null;
  for(const view of liveViews) {
    view.controller.abort();
    if(view.url) URL.revokeObjectURL(view.url);
    const audio=view.tile?.querySelector("audio");
    if(audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
  }
  liveViews=[];
  activeLiveCameraIds=[];
  activeLiveTileCameraIds=[];
  focusedLiveCameraId=null;
  document.querySelector("#live-grid").replaceChildren();
}
function selectedLiveCameras() { return [...document.querySelectorAll("[data-live-camera]:checked")].map(input=>input.value); }
function liveGridSizeFor(cameraCount) { return LIVE_GRID_SIZES.find(size=>size>=cameraCount)||16; }
function currentLiveTiles(size=activeLiveGridSize) {
  const tiles=[...document.querySelectorAll("#live-grid .live-tile")].map(tile=>tile.dataset.cameraId||null);
  if(tiles.length===size)return tiles;
  const ids=activeLiveCameraIds;
  return [...ids,...Array(Math.max(0,size-ids.length)).fill(null)].slice(0,size);
}
function markLiveViewDirty() {
  liveViewDirty=true;
  document.querySelector("#live-unsaved").classList.remove("hidden");
  document.querySelector("#live-message").textContent="Unsaved changes to this view.";
}
function clearLiveViewDirty() { liveViewDirty=false;document.querySelector("#live-unsaved").classList.add("hidden"); }
async function saveLiveWorkspace() {
  if(!currentRole)return;
  const arrangement=activeLiveTileCameraIds.length?{tile_camera_ids:[...activeLiveTileCameraIds],grid_size:activeLiveGridSize}:
    cachedLiveArrangement||{tile_camera_ids:[...Array(activeLiveGridSize).fill(null)],grid_size:activeLiveGridSize};
  const state={camera_ids:arrangement.tile_camera_ids.filter(Boolean),tile_camera_ids:arrangement.tile_camera_ids,
    grid_size:arrangement.grid_size,sidebar_collapsed:document.querySelector("#console-layout").classList.contains("sidebar-collapsed")};
  clearTimeout(workspaceSaveTimer);
  workspaceSaveTimer=setTimeout(async()=>{
    try { await api("/api/live/workspace",{method:"PUT",body:JSON.stringify(state)}); }
    catch(error) { document.querySelector("#live-message").textContent=`Workspace could not be saved: ${error.message}`; }
  },250);
}
async function restoreLiveWorkspace() {
  try {
    const state=await api("/api/live/workspace");
    const layout=document.querySelector("#console-layout"), button=document.querySelector("#sidebar-toggle");
    layout.classList.toggle("sidebar-collapsed",!!state.sidebar_collapsed);
    button.setAttribute("aria-expanded",String(!state.sidebar_collapsed));
    button.setAttribute("aria-label",state.sidebar_collapsed?"Expand camera sidebar":"Collapse camera sidebar");
    if(!state.camera_ids?.length)return;
    for(const input of document.querySelectorAll("[data-live-camera]"))input.checked=state.camera_ids.includes(input.value);
    startLiveViews(state.camera_ids,latestCameras,null,state.grid_size,state.tile_camera_ids);
    clearLiveViewDirty();
  } catch(error) { document.querySelector("#live-message").textContent=`Previous workspace could not be restored: ${error.message}`; }
}
function applySavedView(view) {
  if(activeScreen==="footage") {
    playbackViews.forEach(view=>view.video.pause());
    const playbackPicker=document.querySelector("#playback-camera-picker");
    const cameraLabels=new Map([...playbackPicker.querySelectorAll("[data-playback-camera]")].map(input=>[input.value,input.closest("label")]));
    const labels=cameraLabels;
    const ordered=view.tile_camera_ids.map(id=>id&&labels.get(id)).filter(Boolean);
    playbackPicker.querySelectorAll("fieldset")[1]?.prepend(...ordered);
    for(const input of playbackPicker.querySelectorAll("[data-playback-group]"))input.checked=false;
    playbackManualChoices.clear();
    for(const input of playbackPicker.querySelectorAll("[data-playback-camera]")){
      input.checked=view.camera_ids.includes(input.value);playbackManualChoices.set(input.value,input.checked);
    }
    syncPlaybackGroupChecks();
    document.querySelector("#playback-grid-size").value=String(view.grid_size);
    const unavailable=view.camera_ids.filter(id=>!labels.has(id)).length;
    markPlaybackResultsStale();
    document.querySelector("#playback-status").textContent=`Applied ${view.scope} view “${view.name}”. Camera selection and layout changed; date inputs were kept.${unavailable?` ${unavailable} saved camera(s) are unavailable.`:""}`;
    return;
  }
  setScreen("live");
  document.querySelector("#live-layouts").value=`${view.scope}:${view.name}`;
  document.querySelector("#live-layout-name").value=view.name;
  for(const input of document.querySelectorAll("[data-live-camera]"))input.checked=view.camera_ids.includes(input.value);
  document.querySelector("#live-grid-size").value=String(view.grid_size);
  startLiveViews(view.camera_ids,latestCameras,null,view.grid_size,view.tile_camera_ids);
  clearLiveViewDirty();
  document.querySelector("#live-message").textContent=`Applied ${view.scope} view “${view.name}”.`;
}
function startLiveViews(cameraIds,cameras,focusedCameraId=null,gridSize=null,tileCameraIds=null) {
  const selectedSize=gridSize||Number(document.querySelector("#live-grid-size").value)||4;
  let tiles=tileCameraIds?[...tileCameraIds]:[...cameraIds,...Array(Math.max(0,selectedSize-cameraIds.length)).fill(null)];
  tiles=tiles.slice(0,selectedSize);
  while(tiles.length<selectedSize)tiles.push(null);
  cameraIds=[...new Set(tiles.filter(Boolean))];
  if(!cameraIds.length) { document.querySelector("#live-message").textContent="Choose at least one camera."; return; }
  if(cameraIds.length>16 || cameraIds.length>selectedSize) {
    document.querySelector("#live-message").textContent=`The ${selectedSize}-feed grid cannot show ${cameraIds.length} selected cameras. Choose a larger grid or remove cameras; selections are never dropped.`;
    return;
  }
  stopLiveViews();
  const grid=document.querySelector("#live-grid"), liveMessage=document.querySelector("#live-message");
  activeLiveCameraIds=cameraIds;
  activeLiveTileCameraIds=tiles;
  cachedLiveArrangement={camera_ids:[...cameraIds],tile_camera_ids:[...tiles],grid_size:selectedSize};
  activeLiveGridSize=selectedSize;
  focusedLiveCameraId=focusedCameraId;
  document.querySelector("#live-grid-size").value=String(selectedSize);
  grid.style.setProperty("--live-grid-columns",String(focusedCameraId?1:Math.ceil(Math.sqrt(selectedSize))));
  const displayed=focusedCameraId?[focusedCameraId]:tiles;
  const slots=focusedCameraId?1:selectedSize;
  for(let index=0;index<slots;index++) {
    const cameraId=displayed[index]||null;
    const tile=document.createElement("article"); tile.className="live-tile";tile.tabIndex=0;tile.dataset.tileIndex=String(index);
    tile.addEventListener("dragover",event=>event.preventDefault());
    tile.addEventListener("drop",event=>dropCameraOnTile(event,index,cameras));
    if(!cameraId) {
      const placeholder=document.createElement("div");placeholder.className="live-placeholder";placeholder.textContent="Empty tile · drop a camera here";
      tile.append(placeholder);grid.append(tile);continue;
    }
    tile.dataset.cameraId=cameraId;
    const camera=cameras.find(item=>item.id===cameraId);
    const title=document.createElement("strong"); title.textContent=camera?.name||`Unavailable camera · ${cameraId}`;
    const state=document.createElement("span"); state.className="live-state"; state.textContent=camera?"Loading":"Unavailable";state.setAttribute("role","status");state.setAttribute("aria-live","polite");
    if(!camera){tile.append(title,state);grid.append(tile);continue;}
    const image=document.createElement("img"); image.alt=`Live video from ${camera.name}`;
    const focus=document.createElement("button"); focus.type="button"; focus.textContent=focusedCameraId?"Restore grid":"Expand feed";focus.setAttribute("aria-label",focusedCameraId?"Restore camera grid":`Expand ${camera.name}`);
    focus.addEventListener("click",()=>startLiveViews(activeLiveCameraIds,cameras,focusedCameraId?null:cameraId,activeLiveGridSize,[...activeLiveTileCameraIds]));
    tile.draggable=true;
    tile.addEventListener("dragstart",event=>event.dataTransfer.setData("text/plain",cameraId));
    tile.addEventListener("dblclick",()=>startLiveViews(activeLiveCameraIds,cameras,focusedCameraId?null:cameraId,activeLiveGridSize,[...activeLiveTileCameraIds]));
    tile.addEventListener("keydown",event=>{if(event.key==="Enter"&&event.altKey){event.preventDefault();startLiveViews(activeLiveCameraIds,cameras,focusedLiveCameraId?null:cameraId,activeLiveGridSize,[...activeLiveTileCameraIds]);}});
    tile.append(title,state,focus);
    if(focusedCameraId) {
      const fullscreen=document.createElement("button"); fullscreen.type="button"; fullscreen.textContent="Enter fullscreen";
      fullscreen.addEventListener("click",async()=>{
        if(typeof tile.requestFullscreen!=="function") { liveMessage.textContent="Fullscreen is not supported by this browser."; return; }
        try { await tile.requestFullscreen(); liveMessage.textContent="Focused camera is in fullscreen. Press Escape to return."; }
        catch { liveMessage.textContent="The browser could not enter fullscreen. Check browser permissions and try again."; }
      });
      tile.append(fullscreen);
    }
    tile.append(image);
    if(camera.audio_enabled) {
      const listening=document.createElement("audio");listening.controls=true;listening.preload="none";
      listening.setAttribute("aria-label",`Live audio from ${camera.name}`);
      listening.muted=true;
      listening.title="Audio is muted. Unmute to listen; only one camera can play audio at a time.";
      const audioToggle=document.createElement("button");audioToggle.type="button";audioToggle.textContent="Listen";audioToggle.setAttribute("aria-label",`Listen to ${camera.name}`);
      audioToggle.addEventListener("click",async()=>{
        if(listening.muted){for(const other of document.querySelectorAll("#live-grid audio"))if(other!==listening){other.pause();other.muted=true;}listening.muted=false;try{await listening.play();}catch{listening.muted=true;liveMessage.textContent=`Audio could not start for ${camera.name}.`;}}
        else{listening.pause();listening.muted=true;}
      });
      listening.addEventListener("volumechange",()=>{
        audioToggle.textContent=listening.muted?"Listen":"Mute audio";
        audioToggle.setAttribute("aria-label",listening.muted?`Listen to ${camera.name}`:`Mute audio from ${camera.name}`);
        if(listening.muted)return;
        for(const other of document.querySelectorAll("#live-grid audio")) if(other!==listening){other.pause();other.muted=true;}
      });
      listening.src=`/api/cameras/${encodeURIComponent(camera.id)}/audio/live`;
      tile.append(audioToggle,listening);
    }
    grid.append(tile);
    const view={cameraId,controller:new AbortController(),image,state,url:null,lastFrame:0,startedAt:Date.now(),tile,quality:focusedCameraId?document.querySelector("#live-quality").value:"low",retrying:false}; liveViews.push(view);
    consumeLiveStream(view).catch(error=>{if(!view.controller.signal.aborted){state.textContent="Lost";liveMessage.textContent=`${camera.name}: ${error.message}`;}});
  }
  liveStatusTimer=setInterval(async()=>{
    for(const view of liveViews) {
      if(view.controller.signal.aborted)continue;
      if(Date.now()-(view.lastFrame||view.startedAt)>12000&&!view.retrying){
        view.retrying=true;view.state.textContent="Stale";view.controller.abort();
        if(view.url){URL.revokeObjectURL(view.url);view.url=null;}view.image.removeAttribute("src");
        view.controller=new AbortController();view.lastFrame=0;view.startedAt=Date.now();
        view.state.textContent="Reconnecting";
        consumeLiveStream(view).finally(()=>{view.retrying=false;});
        continue;
      }
      try {
        const status=await api(`/api/cameras/${encodeURIComponent(view.cameraId)}/live/status`);
        if(!view.controller.signal.aborted) view.state.textContent=view.lastFrame?(Date.now()-view.lastFrame>12000?"Stale":status.state==="live"?"Live":status.active_viewers?"Stale":"Reconnecting"):status.state==="stale"?"Stale":"Loading";
      } catch { if(!view.controller.signal.aborted) view.state.textContent="Lost"; }
    }
  },5000);
  markLiveViewDirty();
  saveLiveWorkspace();
}
function waitForLiveRetry(delay,signal){return new Promise(resolve=>{const finish=()=>{clearTimeout(timer);signal.removeEventListener("abort",finish);resolve();};const timer=setTimeout(finish,delay);signal.addEventListener("abort",finish,{once:true});});}
async function consumeLiveStream(view) {
  let attempt=0;
  while(!view.controller.signal.aborted){
    let retryable=true;
    try{
      view.state.textContent=view.lastFrame?"Reconnecting":"Loading";
      const response=await fetch(`/api/cameras/${encodeURIComponent(view.cameraId)}/live?quality=${view.quality}`,{credentials:"same-origin",signal:view.controller.signal});
      if(!response.ok){const body=await response.json().catch(()=>({}));const error=new Error(body.detail||"Live view could not start");retryable=response.status>=500;throw error;}
      if(!response.body)throw new Error("This browser cannot read the live stream");
      const reader=response.body.getReader();let buffer=new Uint8Array(0);attempt=0;
      while(!view.controller.signal.aborted){
        const {value,done}=await reader.read();
        if(done)throw new Error("Camera video stream ended");
        const merged=new Uint8Array(buffer.length+value.length);merged.set(buffer);merged.set(value,buffer.length);buffer=merged;
        while(true){
          let start=-1,end=-1;
          for(let i=0;i<buffer.length-1;i++)if(buffer[i]===255&&buffer[i+1]===216){start=i;break;}
          if(start<0){buffer=buffer.slice(Math.max(0,buffer.length-1));break;}
          for(let i=start+2;i<buffer.length-1;i++)if(buffer[i]===255&&buffer[i+1]===217){end=i+2;break;}
          if(end<0){buffer=buffer.slice(start);if(buffer.length>4*1024*1024)throw new Error("Video frame exceeded size limit");break;}
          const frame=buffer.slice(start,end);buffer=buffer.slice(end);
          const url=URL.createObjectURL(new Blob([frame],{type:"image/jpeg"}));
          if(view.url)URL.revokeObjectURL(view.url);view.url=url;view.image.src=url;view.lastFrame=Date.now();view.startedAt=view.lastFrame;view.retrying=false;view.state.textContent="Live";
        }
        if(buffer.length>4*1024*1024)throw new Error("Live stream exceeded the frame buffer limit");
      }
      return;
    }catch(error){
      if(view.controller.signal.aborted)return;
      if(!retryable){view.retrying=false;view.state.textContent="Lost";return;}
      view.state.textContent="Reconnecting";
      if(view.url){URL.revokeObjectURL(view.url);view.url=null;view.image.removeAttribute("src");}
      await waitForLiveRetry(Math.min(1000*2**attempt,8000),view.controller.signal);
      attempt=Math.min(attempt+1,4);
    }
  }
}
function dropCameraOnTile(event,index,cameras){
  event.preventDefault();const cameraId=event.dataTransfer.getData("text/plain");if(!cameraId)return;
  const next=[...activeLiveCameraIds],current=next.indexOf(cameraId);
  if(current>=0){if(index>=next.length)return;[next[current],next[index]]=[next[index],next[current]];startLiveViews(next,cameras,focusedLiveCameraId,activeLiveGridSize);return;}
  if(next.length>=activeLiveGridSize){document.querySelector("#live-message").textContent="This grid is full. Choose a larger grid or remove a camera first.";return;}
  next.splice(Math.min(index,next.length),0,cameraId);startLiveViews(next,cameras,null,activeLiveGridSize);
  const input=[...document.querySelectorAll("[data-live-camera]")].find(item=>item.value===cameraId);if(input)input.checked=true;
}
function addCameraToLiveGrid(cameraId,cameras){
  if(activeLiveCameraIds.includes(cameraId))return;
  let size=activeLiveGridSize||Number(document.querySelector("#live-grid-size").value)||4;
  if(activeLiveCameraIds.length>=size){size=LIVE_GRID_SIZES.find(item=>item>size)||size;}
  if(activeLiveCameraIds.length>=size){document.querySelector("#live-message").textContent="The 16-feed grid is full. Remove a camera before adding another.";return;}
  if(!activeLiveCameraIds.length)size=Number(document.querySelector("#live-grid-size").value)||4;
  startLiveViews([...activeLiveCameraIds,cameraId],cameras,null,size);
}
function renderPlaybackCameraPicker(cameras,groups=[]) {
  const target=document.querySelector("#playback-camera-picker");
  const groupFieldset=document.createElement("fieldset"),groupLegend=document.createElement("legend");
  groupLegend.textContent="Camera groups";groupFieldset.append(groupLegend);
  groupFieldset.append(...groups.map(group=>{
    const label=document.createElement("label");label.textContent=`${group.name} (${(group.camera_ids||[]).length}) `;
    const input=document.createElement("input");input.type="checkbox";input.dataset.playbackGroup="true";input.value=group.id;
    label.append(input);return label;
  }));
  const cameraFieldset=document.createElement("fieldset"),cameraLegend=document.createElement("legend");
  cameraLegend.textContent="Cameras";cameraFieldset.append(cameraLegend);
  cameraFieldset.append(...cameras.map(camera=>{
    const label=document.createElement("label"); label.textContent=`${camera.name} `;
    const input=document.createElement("input"); input.type="checkbox"; input.dataset.playbackCamera="true"; input.value=camera.id;
    label.append(input); return label;
  }));
  target.replaceChildren(groupFieldset,cameraFieldset);
  syncPlaybackCameraChecks();
}
function syncPlaybackCameraChecks() {
  const selectedByGroups=new Set([...document.querySelectorAll("[data-playback-group]:checked")].flatMap(input=>sidebarGroups.find(group=>group.id===input.value)?.camera_ids||[]));
  for(const input of document.querySelectorAll("[data-playback-camera]"))
    input.checked=playbackManualChoices.has(input.value)?playbackManualChoices.get(input.value):selectedByGroups.has(input.value);
  syncPlaybackGroupChecks();
}
function syncPlaybackGroupChecks() {
  for(const groupInput of document.querySelectorAll("[data-playback-group]")){
    const memberIds=sidebarGroups.find(group=>group.id===groupInput.value)?.camera_ids||[];
    const selected=new Set([...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value));
    const count=memberIds.filter(id=>selected.has(id)).length;
    groupInput.checked=memberIds.length>0&&count===memberIds.length;
    groupInput.indeterminate=count>0&&count<memberIds.length;
  }
}
function markPlaybackResultsStale() {
  if(!playbackTimeline)return;
  playbackResultsStale=true;
  playbackViews.forEach(view=>view.video.pause());
  document.querySelector("#playback-stale").classList.remove("hidden");
  document.querySelector("#playback-status").textContent="Search inputs changed. Results are stale until you load the timeline again.";
}
function stopSynchronizedPlayback() {
  playbackViews.forEach(view=>{view.video.pause();view.video.removeAttribute("src");view.video.load();});
  playbackViews=[]; playbackTimeline=null; playbackMaster=null;playbackWindow=null;
  document.querySelector("#playback-grid")?.replaceChildren();
  const cursor=document.querySelector("#playback-cursor"); if(cursor) cursor.disabled=true;
}
function playbackMilliseconds(value) { return new Date(value).getTime(); }
function currentPlaybackWindow() {
  if(playbackWindow)return playbackWindow;
  return playbackTimeline?{start:playbackMilliseconds(playbackTimeline.start),end:playbackMilliseconds(playbackTimeline.end)}:null;
}
function playbackPosition(value) {
  const window=currentPlaybackWindow();if(!window||window.end<=window.start)return null;
  const time=playbackMilliseconds(value);if(time<window.start||time>window.end)return null;
  return Math.max(0,Math.min(100,(time-window.start)/(window.end-window.start)*100));
}
function renderPlaybackRuler() {
  const ruler=document.querySelector("#playback-ruler"),window=currentPlaybackWindow();
  if(!ruler||!window)return;
  ruler.replaceChildren();
  ruler.tabIndex=0;ruler.setAttribute("aria-valuemin",String(window.start));ruler.setAttribute("aria-valuemax",String(window.end));
  ruler.setAttribute("aria-valuenow",String(Number(document.querySelector("#playback-cursor")?.dataset.time||window.start)));
  ruler.setAttribute("aria-valuetext",formatDateTime(Number(document.querySelector("#playback-cursor")?.dataset.time||window.start)));
  for(let index=0;index<=4;index++){
    const at=window.start+(window.end-window.start)*index/4,label=document.createElement("span");
    label.className="playback-ruler-tick";label.style.left=`${index*25}%`;label.textContent=formatDateTime(at,true);ruler.append(label);
  }
  ruler.setAttribute("aria-label",`Timeline from ${formatDateTime(window.start)} to ${formatDateTime(window.end)}. Drag to pan; click to seek.`);
}
function fitPlaybackTimeline() {
  if(!playbackTimeline)return;
  playbackWindow={start:playbackMilliseconds(playbackTimeline.start),end:playbackMilliseconds(playbackTimeline.end)};
  renderPlaybackRuler();renderPlaybackTracks();
}
function zoomPlaybackTimeline(factor) {
  const window=currentPlaybackWindow();if(!window||!playbackTimeline)return;
  const rangeStart=playbackMilliseconds(playbackTimeline.start),rangeEnd=playbackMilliseconds(playbackTimeline.end),range=rangeEnd-rangeStart;
  const center=Number(document.querySelector("#playback-cursor").dataset.time||((window.start+window.end)/2));
  const nextSpan=Math.max(60000,Math.min(range,(window.end-window.start)*factor));
  let start=center-nextSpan/2,end=center+nextSpan/2;
  if(start<rangeStart){end+=rangeStart-start;start=rangeStart;}
  if(end>rangeEnd){start-=end-rangeEnd;end=rangeEnd;}
  playbackWindow={start:Math.max(rangeStart,start),end:Math.min(rangeEnd,end)};
  renderPlaybackRuler();renderPlaybackTracks();
}
function seekPlaybackAtClientX(clientX,element) {
  const window=currentPlaybackWindow();if(!window)return;
  const rect=element.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(clientX-rect.left)/rect.width));
  seekSynchronizedPlayback(window.start+(window.end-window.start)*ratio);
}
function setupPlaybackRulerGestures() {
  const ruler=document.querySelector("#playback-ruler");if(!ruler)return;
  let drag=null;
  ruler.addEventListener("keydown",event=>{
    if(!playbackTimeline||!playbackWindow||!["ArrowLeft","ArrowRight","PageUp","PageDown"].includes(event.key))return;
    event.preventDefault();
    const current=Number(document.querySelector("#playback-cursor").dataset.time||playbackWindow.start);
    const step=(playbackWindow.end-playbackWindow.start)*(event.key.startsWith("Page")?.25:.01)*(event.key.endsWith("Left")||event.key==="PageDown"?-1:1);
    seekSynchronizedPlayback(current+step);
  });
  ruler.addEventListener("pointerdown",event=>{
    if(!playbackTimeline)return;
    ruler.setPointerCapture(event.pointerId);
    const window=currentPlaybackWindow();drag={id:event.pointerId,x:event.clientX,start:window.start,end:window.end,moved:false};
  });
  ruler.addEventListener("pointermove",event=>{
    if(!drag||drag.id!==event.pointerId)return;
    const dx=event.clientX-drag.x;if(Math.abs(dx)>4)drag.moved=true;
    if(!drag.moved)return;
    const span=drag.end-drag.start,shift=dx/ruler.getBoundingClientRect().width*span;
    const rangeStart=playbackMilliseconds(playbackTimeline.start),rangeEnd=playbackMilliseconds(playbackTimeline.end);
    let start=drag.start-shift,end=drag.end-shift;
    if(start<rangeStart){end+=rangeStart-start;start=rangeStart;}
    if(end>rangeEnd){start-=end-rangeEnd;end=rangeEnd;}
    playbackWindow={start:Math.max(rangeStart,start),end:Math.min(rangeEnd,end)};
    renderPlaybackRuler();renderPlaybackTracks();
  });
  const finish=event=>{
    if(!drag||drag.id!==event.pointerId)return;
    if(!drag.moved)seekPlaybackAtClientX(event.clientX,ruler);
    drag=null;
  };
  ruler.addEventListener("pointerup",finish);ruler.addEventListener("pointercancel",()=>{drag=null;});
}
function renderPlaybackTracks() {
  if(!playbackViews.length)return;
  for(const view of playbackViews){
    const track=view.tile?.querySelector(".playback-track");if(!track)continue;
    const next=renderPlaybackTrack(view);track.replaceWith(next);
    const list=view.tile.querySelector(".playback-motion-list");if(list)list.replaceWith(renderPlaybackMotionList(view));
  }
}
function renderPlaybackTrack(view) {
  const track=document.createElement("div");track.className="playback-track";track.setAttribute("aria-label",`Recording coverage and motion timeline for ${view.name}`);
  const window=currentPlaybackWindow();if(!window||window.end<=window.start)return track;
  const pct=ms=>Math.max(0,Math.min(100,(ms-window.start)/(window.end-window.start)*100));
  for(const segment of view.segments){
    const left=playbackMilliseconds(segment.starts_at),right=playbackMilliseconds(segment.ends_at);
    if(right<window.start||left>window.end)continue;
    const available=document.createElement("span");available.className="playback-recording";available.style.left=`${pct(left)}%`;available.style.width=`${Math.max(.4,pct(right)-pct(left))}%`;available.title=`Recorded footage · ${formatDateTime(segment.starts_at)} to ${formatDateTime(segment.ends_at)}`;track.append(available);
  }
  for(const gap of view.gaps){
    const left=playbackMilliseconds(gap.starts_at),right=playbackMilliseconds(gap.ends_at);
    if(right<window.start||left>window.end)continue;
    const mark=document.createElement("button");mark.type="button";mark.className=`playback-gap ${gap.state}`;mark.style.left=`${pct(left)}%`;mark.style.width=`${Math.max(.4,pct(right)-pct(left))}%`;mark.title=`${gap.state}: ${gap.reason}`;mark.setAttribute("aria-label",mark.title);mark.addEventListener("click",event=>{event.stopPropagation();seekSynchronizedPlayback(left);});track.append(mark);
  }
  for(const event of view.motion_events){
    const at=playbackMilliseconds(event.source_event_at),position=playbackPosition(event.source_event_at);if(position===null)continue;
    const marker=document.createElement("button");marker.type="button";marker.className="playback-motion";marker.style.left=`${position}%`;marker.title=`Motion · ${view.name} · ${formatDateTime(event.source_event_at)}`;marker.setAttribute("aria-label",marker.title);marker.addEventListener("click",click=>{click.stopPropagation();seekSynchronizedPlayback(at);});track.append(marker);
  }
  track.addEventListener("click",event=>seekPlaybackAtClientX(event.clientX,track));
  return track;
}
function renderPlaybackMotionList(view) {
  const list=document.createElement("div");list.className="playback-motion-list";
  if(!view.motion_events.length){const note=document.createElement("p");note.textContent="No motion events in this range.";list.append(note);return list;}
  for(const event of view.motion_events){
    const start=event.actual_start||event.requested_start||event.source_event_at,end=event.actual_end||event.requested_end;
    const duration=end?Math.max(0,(playbackMilliseconds(end)-playbackMilliseconds(start))/1000):0;
    const button=document.createElement("button");button.type="button";button.className="playback-motion-event";button.textContent=`Motion · ${view.name} · ${formatDateTime(event.source_event_at)} · ${duration.toFixed(0)} sec · ${event.status}`;
    button.addEventListener("click",()=>seekSynchronizedPlayback(playbackMilliseconds(event.source_event_at)));
    list.append(button);
  }
  return list;
}
function playbackSegmentAt(view,timeMs) {
  return view.segments.find(segment=>playbackMilliseconds(segment.starts_at)<=timeMs && timeMs<playbackMilliseconds(segment.ends_at));
}
function playbackSetCursor(timeMs) {
  if(!playbackTimeline) return;
  const start=playbackMilliseconds(playbackTimeline.start), end=playbackMilliseconds(playbackTimeline.end);
  const bounded=Math.max(start,Math.min(end,timeMs));
  const cursor=document.querySelector("#playback-cursor");
  cursor.value=String(Math.round((bounded-start)/(end-start)*1000));
  cursor.dataset.time=String(bounded);
  return bounded;
}
function playbackAbsTime(view) {
  if(!view.segment) return null;
  return playbackMilliseconds(view.segment.starts_at)+view.video.currentTime*1000;
}
function playbackSetSegment(view,timeMs,playing=false) {
  const segment=playbackSegmentAt(view,timeMs);
  if(!segment) {
    if(view.segment){view.video.pause();view.video.removeAttribute("src");view.video.load();view.segment=null;}
    view.state.textContent="No footage at this time";
    return false;
  }
  const changed=!view.segment||view.segment.id!==segment.id;
  view.segment=segment; view.targetMs=timeMs;
  const offset=Math.max(0,(timeMs-playbackMilliseconds(segment.starts_at))/1000);
  if(changed) {
    view.video.src=segment.media_url; view.video.load(); view.state.textContent="Loading interval…";
    view.video.onloadedmetadata=()=>{if(!view.video.isConnected)return;view.video.currentTime=Math.min(offset,Math.max(0,view.video.duration-0.05));view.video.playbackRate=Number(document.querySelector("#playback-rate").value);view.state.textContent="Ready";if(playing)view.video.play().catch(()=>{view.state.textContent="Press Play to start";});};
  } else {
    const drift=Math.abs(view.video.currentTime-offset);
    if(drift>0.3)view.video.currentTime=offset;
    view.video.playbackRate=Number(document.querySelector("#playback-rate").value);
    if(playing&&view.video.paused)view.video.play().catch(()=>{view.state.textContent="Press Play to start";});
    if(!playing&&!view.video.paused)view.video.pause();
  }
  view.state.textContent=changed?"Loading interval…":"In sync";
  return true;
}
function seekSynchronizedPlayback(timeMs,playing=false,master=null) {
  if(!playbackTimeline)return;
  const bounded=playbackSetCursor(timeMs); playbackMaster=master||playbackMaster;
  playbackSyncGuard=true;
  playbackViews.forEach(view=>playbackSetSegment(view,bounded,playing));
  playbackSyncGuard=false;
  updatePlaybackDrift();
}
function updatePlaybackDrift() {
  const times=playbackViews.map(playbackAbsTime).filter(value=>value!==null);
  const drift=times.length>1?Math.max(...times)-Math.min(...times):0;
  const label=document.querySelector("#playback-status");
  if(playbackTimeline) {
    const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start),ruler=document.querySelector("#playback-ruler");
    ruler?.setAttribute("aria-valuenow",String(at));ruler?.setAttribute("aria-valuetext",formatDateTime(at));
    label.textContent=`Shared time ${formatDateTime(at,true)} · measured playback drift ${Math.round(drift)} ms`;
  }
}
function renderPlaybackCamera(view) {
  const tile=document.createElement("article"); tile.className="playback-camera";
  const heading=document.createElement("h4"); heading.textContent=view.name;
  const state=document.createElement("p"); state.className="playback-status"; state.textContent=view.segments.length?"Ready":"No indexed footage in this interval";
  const video=document.createElement("video"); video.controls=true; video.playsInline=true; video.preload="metadata"; video.setAttribute("aria-label",`Synchronized playback for ${view.name}`);
  const controls=document.createElement("div");
  const snapshot=document.createElement("button"); snapshot.type="button"; snapshot.textContent="Download snapshot";
  snapshot.addEventListener("click",async()=>{
    const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start);
    const segment=playbackSegmentAt(view,at);
    if(!segment){state.textContent="No snapshot is available at the shared time.";return;}
    try {const response=await fetch(`${segment.media_url.replace(/\/media$/,"")}/snapshot?at=${encodeURIComponent(new Date(at).toISOString())}`,{credentials:"same-origin"});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||"Snapshot is unavailable");}const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=`${view.name.replace(/[^a-z0-9_-]+/gi,"_")}-${new Date(at).toISOString().replace(/[:.]/g,"-")}.jpg`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);state.textContent="Snapshot downloaded.";}catch(error){state.textContent=error.message;}
  });
  const fullscreen=document.createElement("button"); fullscreen.type="button"; fullscreen.textContent="Fullscreen";
  fullscreen.addEventListener("click",()=>{if(!tile.requestFullscreen){state.textContent="Fullscreen is not supported by this browser.";return;}tile.requestFullscreen().catch(()=>{state.textContent="Fullscreen was blocked by the browser.";});});
  controls.append(snapshot,fullscreen); tile.append(heading,state,video,controls);
  const track=renderPlaybackTrack(view),motionList=renderPlaybackMotionList(view);
  tile.append(track);
  tile.append(motionList);
  view.video=video;view.state=state;view.tile=tile;
  video.addEventListener("play",()=>{if(playbackSyncGuard)return;playbackMaster=view;const time=playbackAbsTime(view);if(time!==null)seekSynchronizedPlayback(time,true,view);});
  video.addEventListener("pause",()=>{if(playbackSyncGuard)return;playbackViews.forEach(other=>{if(other!==view)other.video.pause();});});
  video.addEventListener("seeking",()=>{if(playbackSyncGuard)return;const time=playbackAbsTime(view);if(time!==null)seekSynchronizedPlayback(time,!view.video.paused,view);});
  video.addEventListener("timeupdate",()=>{if(playbackSyncGuard||playbackMaster!==view)return;const time=playbackAbsTime(view);if(time===null)return;playbackSetCursor(time);playbackSyncGuard=true;playbackViews.filter(other=>other!==view).forEach(other=>playbackSetSegment(other,time,!view.video.paused));playbackSyncGuard=false;updatePlaybackDrift();});
  return tile;
}
async function loadSynchronizedPlayback() {
  const camera_ids=[...new Set([...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value))];
  const startInput=document.querySelector("#playback-start").value,endInput=document.querySelector("#playback-end").value;
  const label=document.querySelector("#playback-status");
  try {
    if(!camera_ids.length||camera_ids.length>8)throw new Error("Choose between one and eight unique cameras.");
    if(!startInput||!endInput)throw new Error("Enter both a start and end time.");
    const start=resolveSearchLocalTime("start"),end=resolveSearchLocalTime("end");
    const startMs=playbackMilliseconds(start),endMs=playbackMilliseconds(end);
    if(endMs<=startMs)throw new Error("The end time must be later than the start time.");
    if(endMs-startMs>24*60*60*1000)throw new Error("Search ranges are limited to 24 hours.");
    playbackViews.forEach(view=>view.video.pause());
    label.textContent="Loading indexed coverage and motion events…";
    const result=await api("/api/playback/timeline",{method:"POST",body:JSON.stringify({camera_ids,start,end})});
    stopSynchronizedPlayback();
    playbackTimeline=result;const cursor=document.querySelector("#playback-cursor");cursor.disabled=false;cursor.value="0";cursor.dataset.time=String(playbackMilliseconds(result.start));
    playbackWindow={start:playbackMilliseconds(result.start),end:playbackMilliseconds(result.end)};
    const grid=document.querySelector("#playback-grid");
    grid.style.setProperty("--playback-grid-columns",String(Math.ceil(Math.sqrt(Number(document.querySelector("#playback-grid-size").value)||4))));
    playbackViews=result.cameras.map(camera=>({cameraId:camera.camera_id,name:camera.name,segments:camera.segments,gaps:camera.gaps,motion_events:camera.motion_events,coverage:camera.coverage,video:null,state:null,segment:null}));
    grid.replaceChildren(...playbackViews.map(renderPlaybackCamera));
    renderPlaybackRuler();
    const earliest=playbackViews.flatMap(view=>view.segments.map(segment=>playbackMilliseconds(segment.starts_at))).filter(Number.isFinite);
    seekSynchronizedPlayback(earliest.length?Math.min(...earliest):playbackMilliseconds(result.start));
    playbackResultsStale=false;document.querySelector("#playback-stale").classList.add("hidden");
    label.textContent=`Loaded ${result.cameras.length} camera timeline(s). Playback is paused at the earliest indexed recording; events and gaps are aligned to the same search range.`;
  }catch(error){label.textContent=error.message;}
}
async function refreshExports() {
  const target=document.querySelector("#export-jobs");
  if(!target)return;
  try {
    const {exports}=await api("/api/exports");
    target.replaceChildren(...exports.map(job=>{
      const row=document.createElement("li");
      const state=document.createElement("span");
      state.textContent=`${job.id.slice(0,8)} · ${job.status} · ${formatDateTime(job.start)} – ${formatDateTime(job.end)}${job.cancel_requested?" · cancelling":""}${job.error?` · ${job.error}`:""}`;
      row.append(state);
      if(job.download_url){const download=document.createElement("a");download.href=job.download_url;download.textContent=" Download ZIP";row.append(download);}
      if(job.status==="queued"||job.status==="running"){
        const cancel=document.createElement("button");cancel.type="button";cancel.textContent="Cancel";
        cancel.addEventListener("click",async()=>{try{await api(`/api/exports/${encodeURIComponent(job.id)}/cancel`,{method:"POST"});await refreshExports();}catch(error){document.querySelector("#export-message").textContent=error.message;}});
        row.append(cancel);
      }
      return row;
    }));
    if(exports.some(job=>job.status==="queued"||job.status==="running")){
      if(!exportPollTimer)exportPollTimer=setInterval(()=>refreshExports(),2500);
    }else if(exportPollTimer){clearInterval(exportPollTimer);exportPollTimer=null;}
  }catch(error){document.querySelector("#export-message").textContent=error.message;}
}
async function requestPlaybackExport() {
  const message=document.querySelector("#export-message"),button=document.querySelector("#playback-export");
  const camera_ids=[...new Set([...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value))];
  const start=document.querySelector("#playback-start").value,end=document.querySelector("#playback-end").value;
  try {
    if(!camera_ids.length||camera_ids.length>8)throw new Error("Choose between one and eight cameras.");
    if(!start||!end)throw new Error("Enter both a start and end time.");
    button.disabled=true;message.textContent="Submitting export…";
    const startInstant=resolveSearchLocalTime("start"),endInstant=resolveSearchLocalTime("end");
    if(playbackMilliseconds(endInstant)<=playbackMilliseconds(startInstant))throw new Error("The end time must be later than the start time.");
    if(playbackMilliseconds(endInstant)-playbackMilliseconds(startInstant)>24*60*60*1000)throw new Error("Export ranges are limited to 24 hours.");
    const job=await api("/api/exports",{method:"POST",body:JSON.stringify({camera_ids,start:startInstant,end:endInstant})});
    message.textContent=`Export ${job.id.slice(0,8)} queued. You can leave this page; the job will continue.`;
    await refreshExports();
  }catch(error){message.textContent=error.message;}
  finally{button.disabled=false;}
}
async function refreshDiagnostics() {
  const state = await api("/api/diagnostics/status");
  const button = document.querySelector("#diagnostics-toggle");
  button.textContent = state.enabled ? "Stop diagnostics capture" : "Enable diagnostics capture";
  document.querySelector("#diagnostics-message").textContent = state.enabled
    ? `Capture is on until ${formatDateTime(state.capture_until)} · ${state.event_count} event(s) collected.`
    : `${state.event_count} event(s) available for download. Capture is off.`;
}
async function refreshGroups() {
  const {groups}=await api("/api/camera-groups");
  renderGroupManagement(groups);
  const select=document.querySelector("#bulk-group");
  const options=[new Option("Leave groups unchanged","__unchanged__"),new Option("Remove from group","")];
  for (const group of groups) options.push(new Option(group.name,group.name));
  select.replaceChildren(...options);
  const scheduleSelect=document.querySelector("#schedule-group");
  scheduleSelect.replaceChildren(...(groups.length ? groups.map(group=>new Option(group.name,group.id)) : [new Option("Create a group first","")]));
  try { const runtime=await api("/api/ai/settings"); document.querySelector("#ai-max-active").value=runtime.max_active_cameras; } catch {}
}
function renderGroupManagement(groups) {
  const target=document.querySelector("#group-management-list");
  target.replaceChildren(...groups.map(group=>{
    const card=document.createElement("article"),heading=document.createElement("h4");
    heading.textContent=group.name+" · "+group.camera_count+" camera(s)";
    const nameLabel=document.createElement("label"),name=document.createElement("input");
    nameLabel.textContent="Group name ";name.value=group.name;name.maxLength=80;nameLabel.append(name);
    const rename=document.createElement("button");rename.type="button";rename.textContent="Save group name";
    rename.addEventListener("click",async()=>{
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id),{method:"PATCH",body:JSON.stringify({name:name.value})});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    const members=new Set(group.camera_ids||[]),fieldset=document.createElement("fieldset"),legend=document.createElement("legend");
    legend.textContent="Browsing members";fieldset.append(legend);
    for(const camera of [...latestCameras].sort((a,b)=>a.name.localeCompare(b.name))){
      const label=document.createElement("label"),checkbox=document.createElement("input");
      checkbox.type="checkbox";checkbox.value=camera.id;checkbox.checked=members.has(camera.id);
      if(camera.group_id===group.id){checkbox.disabled=true;checkbox.title="This is the camera’s policy group and stays in its browsing group automatically.";}
      label.append(checkbox,document.createTextNode(" "+camera.name));fieldset.append(label);
    }
    const saveMembers=document.createElement("button");saveMembers.type="button";saveMembers.textContent="Save browsing members";
    saveMembers.addEventListener("click",async()=>{
      const camera_ids=[...fieldset.querySelectorAll('input[type="checkbox"]:checked')].map(input=>input.value);
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id)+"/members",{method:"PUT",body:JSON.stringify({camera_ids})});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    const remove=document.createElement("button");remove.type="button";remove.textContent="Delete group";
    remove.addEventListener("click",async()=>{
      if(!window.confirm("Delete browsing group “"+group.name+"”? Groups that still own camera policies must be reassigned first."))return;
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id),{method:"DELETE"});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    card.append(heading,nameLabel,rename,fieldset,saveMembers,remove);return card;
  }));
  if(!groups.length)target.textContent="No camera groups yet.";
}
async function refreshDiscovery() {
  const [{candidates}, inventory] = await Promise.all([api("/api/discovery/candidates"), api("/api/cameras")]);
  const target=document.querySelector("#discovery-candidates");
  if (!candidates.length) { target.textContent="No discovered cameras awaiting review."; return; }
  target.replaceChildren(...candidates.map(candidate => {
    const card=document.createElement("article");
    const title=document.createElement("h4");
    const candidateAddress = (() => { try { return new URL(candidate.service_urls[0]).hostname; } catch { return ""; } })();
    title.textContent=candidate.device_uuid.startsWith("ipscan:") ? `Camera at ${candidateAddress}` : `ONVIF device ${candidate.device_uuid}`;
    const state=document.createElement("p");
    state.textContent=candidate.linked_camera_id ? `Already linked to camera ${candidate.linked_camera_id}` : `First seen ${formatDateTime(candidate.first_seen_at)} · Last seen ${formatDateTime(candidate.last_seen_at)}`;
    const services=document.createElement("p"); services.textContent=`Device service: ${candidate.service_urls.join(", ") || "unavailable"}`;
    const capabilities=document.createElement("p");
    capabilities.textContent=`Capabilities · ${Object.entries(candidate.capabilities).map(([name,result])=>`${name}: ${result}`).join(" · ")}`;
    card.append(title,state,services,capabilities);
    if (!candidate.linked_camera_id) {
      const nameLabel=document.createElement("label"); nameLabel.textContent="Camera name";
      const name=document.createElement("input"); name.maxLength=120; name.required=true; name.value=`Camera ${candidate.device_uuid.slice(0,8)}`;
      const addressLabel=document.createElement("label"); addressLabel.textContent="Camera IP address";
      const address=document.createElement("input"); address.inputMode="numeric"; address.autocomplete="off"; address.required=true; address.value=candidateAddress;
      const portLabel=document.createElement("label"); portLabel.textContent="Camera ONVIF port";
      const port=document.createElement("input"); port.type="number"; port.min="1"; port.max="65535"; port.value="80";
      const brandLabel=document.createElement("label"); brandLabel.textContent="Camera brand";
      const brand=document.createElement("select");
      for(const [value,label] of [["auto","Auto-detect / ONVIF"],["acti","ACTi"],["axis","Axis"],["bosch","Bosch"],["dahua","Dahua"],["hanwha","Hanwha"],["hikvision","Hikvision"],["reolink","Reolink"],["samsung","Samsung"],["uniview","Uniview"],["other","Other / generic ONVIF"]]) { const option=new Option(label,value); brand.append(option); }
      const userLabel=document.createElement("label"); userLabel.textContent="Camera username (optional)";
      const user=document.createElement("input"); user.autocomplete="off";
      const passLabel=document.createElement("label"); passLabel.textContent="Camera password (optional)";
      const pass=document.createElement("input"); pass.type="password"; pass.autocomplete="new-password";
      const confirmLabel=document.createElement("label"); confirmLabel.textContent="Confirm camera password";
      const confirm=document.createElement("input"); confirm.type="password"; confirm.autocomplete="new-password";
      const serviceLabel=document.createElement("label"); serviceLabel.textContent="ONVIF device endpoint URL (optional custom path)";
      const serviceInput=document.createElement("input"); serviceInput.type="url"; serviceInput.autocomplete="off"; serviceInput.value=candidate.service_urls[0]||"";
      const loadProfiles=document.createElement("button"); loadProfiles.type="button"; loadProfiles.textContent="Load camera endpoints";
      const profileLabel=document.createElement("label"); profileLabel.textContent="Camera stream endpoint";
      const profiles=document.createElement("select"); profiles.append(new Option("Load endpoints to choose a stream",""));
      let serviceUrl=serviceInput.value;
      serviceInput.addEventListener("input",()=>{ serviceUrl=serviceInput.value.trim(); profiles.replaceChildren(new Option("Load endpoints to choose a stream","")); });
      loadProfiles.addEventListener("click",async()=>{
        loadProfiles.disabled=true;
        try {
          if(pass.value!==confirm.value) throw new Error("Camera passwords do not match");
          const portNumber=Number(port.value); if(!Number.isInteger(portNumber)||portNumber<1||portNumber>65535) throw new Error("Enter a valid camera ONVIF port");
          const result=await api("/api/cameras/onvif-profiles",{method:"POST",body:JSON.stringify({camera_ip:address.value,camera_username:user.value||null,camera_password:pass.value||null,camera_brand:brand.value,device_service_url:serviceInput.value||`http://${address.value}:${portNumber}/onvif/device_service`})});
          serviceUrl=result.device_service_url;
          profiles.replaceChildren(...result.profiles.map(profile=>new Option(profile.name,profile.token)));
          if(result.profiles.length>1) profiles.prepend(new Option("Select stream profile",""));
        } catch(error) { document.querySelector("#discovery-message").textContent=error.message; }
        finally { loadProfiles.disabled=false; }
      });
      const add=document.createElement("button"); add.type="button"; add.textContent="Test and add camera";
      add.addEventListener("click",async()=>{
        try {
          if(pass.value!==confirm.value) throw new Error("Camera passwords do not match");
          if(!profiles.value) throw new Error("Load camera endpoints and select a stream profile first");
          const camera=await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/add`,{method:"POST",body:JSON.stringify({name:name.value,camera_ip:address.value,camera_username:user.value||null,camera_password:pass.value||null,camera_brand:brand.value,onvif_profile_token:profiles.value,device_service_url:serviceUrl,motion_username:user.value||null,motion_password:pass.value||null,recording_mode:"motion",recording_enabled:false})});
          document.querySelector("#discovery-message").textContent=`${camera.name} added with recording disabled. Configure its event source and explicitly enable recording when ready.`;
          await refresh();
        } catch(error) { document.querySelector("#discovery-message").textContent=error.message; }
      });
      card.append(nameLabel,name,addressLabel,address,portLabel,port,brandLabel,brand,userLabel,user,passLabel,pass,confirmLabel,confirm,serviceLabel,serviceInput,loadProfiles,profileLabel,profiles,add);
      if (inventory.cameras.length) {
        const linkLabel=document.createElement("label"); linkLabel.textContent="Or link this device to an existing manually added camera";
        const select=document.createElement("select");
        for (const camera of inventory.cameras) {
          const option=document.createElement("option"); option.value=camera.id; option.textContent=camera.name; select.append(option);
        }
        const link=document.createElement("button"); link.type="button"; link.textContent="Link identity to selected camera";
        link.addEventListener("click",async()=>{
          try {
            await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/link`,{method:"POST",body:JSON.stringify({camera_id:select.value})});
            document.querySelector("#discovery-message").textContent="Discovered identity linked to the existing camera; recorded footage and camera identity were preserved.";
            await refresh();
          } catch(error) { document.querySelector("#discovery-message").textContent=error.message; }
        });
        card.append(linkLabel,select,link);
      }
    }
    return card;
  }));
}
async function refreshSessions() {
  const target = document.querySelector("#sessions-list");
  const {sessions} = await api("/api/admin/sessions");
  target.replaceChildren(...sessions.map(session => {
    const row=document.createElement("p");
    const text=document.createElement("span");
    text.textContent=`${session.username} (${session.role}) · expires ${formatDateTime(session.expires_at)} `;
    const end=document.createElement("button"); end.type="button"; end.textContent="End session";
    end.addEventListener("click", async () => { await api(`/api/admin/sessions/${encodeURIComponent(session.session_id)}`, {method:"DELETE"}); await refreshSessions(); });
    row.append(text,end); return row;
  }));
  if (!sessions.length) target.textContent="No active sessions.";
}
function renderCameras(cameras) {
  const list = document.querySelector("#camera-list");
  if (!cameras.length) { list.textContent = "No cameras have been added."; return; }
  list.replaceChildren(...cameras.map(camera => {
    const card = document.createElement("article");
    const title = document.createElement("h3"); title.textContent = camera.name;
    if (currentRole === "administrator") {
      const pickLabel=document.createElement("label"); pickLabel.textContent="Select for bulk changes ";
      const pick=document.createElement("input"); pick.type="checkbox"; pick.dataset.cameraSelect="true"; pick.value=camera.id;
      pickLabel.append(pick); card.append(pickLabel);
    }
    const summary = document.createElement("p");
    const video = camera.video || {};
    const effectiveMode=camera.effective_recording_mode || camera.recording_mode;
    const policyLabel=camera.recording_policy_source === "group" ? "group schedule" : camera.schedule_timezone ? "camera schedule" : "camera policy";
    const healthSummary=camera.recording_health||{};
    summary.textContent = (camera.group_name ? camera.group_name + " · " : "") + "storage " + (camera.storage_volume_id||"default") + " · " + (camera.recording_enabled ? "recording enabled" : "recording disabled") + " · effective " + effectiveMode + " (" + policyLabel + ") · " + camera.recording_status + " · coverage " + (healthSummary.state||"unknown") + (healthSummary.detail ? " — " + healthSummary.detail : "") + (healthSummary.last_video_at ? " · video seen " + healthSummary.last_video_at : "") + (healthSummary.last_footage_write_at ? " · footage written " + healthSummary.last_footage_write_at : "") + (camera.recording_mode === "motion" ? " · event source " + camera.motion_source_status + " · server fallback " + (camera.motion_fallback_enabled ? camera.server_motion_status : "disabled") : "") + " · audio " + (camera.audio_status||"unknown") + " · " + (video.codec || "unknown codec") + (video.width && video.height ? " " + video.width + "×" + video.height : "");
    const retentionSummary=camera.retention_coverage||{};
    summary.textContent += ` · retention ${camera.retention_days} days (${camera.retention_policy_source||"default"}; ${retentionSummary.achieved_days||0} days achieved` + (retentionSummary.projected_days_at_current_activity == null ? ")" : `; ${retentionSummary.projected_days_at_current_activity} days projected at current activity)`);
    card.append(title, summary);
    if(currentRole === "administrator") {
      const audioLabel=document.createElement("label");audioLabel.textContent="Enable audio recording and operator listening ";
      const audioEnabled=document.createElement("input");audioEnabled.type="checkbox";audioEnabled.checked=Boolean(camera.audio_enabled);audioEnabled.disabled=camera.audio_supported!==true;
      audioLabel.append(audioEnabled);
      const saveAudio=document.createElement("button");saveAudio.type="button";saveAudio.textContent="Apply audio setting";
      saveAudio.addEventListener("click",async()=>{try{await api(`/api/cameras/${encodeURIComponent(camera.id)}`,{method:"PATCH",body:JSON.stringify({audio_enabled:audioEnabled.checked})});await refresh();}catch(error){document.querySelector("#camera-message").textContent=error.message;}});
      card.append(audioLabel,saveAudio);
      if(camera.audio_supported==null) {
        const probeAudio=document.createElement("button");probeAudio.type="button";probeAudio.textContent="Probe camera audio";
        probeAudio.addEventListener("click",async()=>{try{const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/audio/probe`,{method:"POST"});document.querySelector("#camera-message").textContent=result.detail;await refresh();}catch(error){document.querySelector("#camera-message").textContent=error.message;}});
        card.append(probeAudio);
      }
    }
    if(currentRole === "administrator") {
      const retentionLabel=document.createElement("label"); retentionLabel.textContent="Camera retention override (days)";
      const retentionDays=document.createElement("input"); retentionDays.type="number"; retentionDays.min="1"; retentionDays.max="3650"; retentionDays.value=String(camera.retention_days||30);
      const saveRetention=document.createElement("button"); saveRetention.type="button"; saveRetention.textContent="Save camera retention";
      saveRetention.addEventListener("click",async()=>{try{await api(`/api/cameras/${encodeURIComponent(camera.id)}/retention`,{method:"PUT",body:JSON.stringify({retention_days:Number(retentionDays.value)})});await refresh();}catch(error){document.querySelector("#camera-message").textContent=error.message;}});
      const inheritRetention=document.createElement("button"); inheritRetention.type="button"; inheritRetention.textContent="Inherit group retention";
      inheritRetention.addEventListener("click",async()=>{try{await api(`/api/cameras/${encodeURIComponent(camera.id)}/retention`,{method:"DELETE"});await refresh();}catch(error){document.querySelector("#camera-message").textContent=error.message;}});
      card.append(retentionLabel,retentionDays,saveRetention,inheritRetention);
    }
    if (camera.last_error) { const note=document.createElement("p"); note.textContent=camera.last_error; card.append(note); }
    const button = document.createElement("button"); button.type="button"; button.textContent="Find recorded footage";
    button.addEventListener("click", () => loadSegments(camera)); card.append(button);
    if (currentRole !== "administrator") return card;
    const mode=document.createElement("select"); mode.setAttribute("aria-label",`Recording mode for ${camera.name}`);
    for (const [value,label] of [["motion","Motion (ONVIF events)"],["continuous","Continuous"],["disabled","Disabled"]]) {
      const option=document.createElement("option"); option.value=value; option.textContent=label; mode.append(option);
    }
    mode.value=camera.recording_mode;
    const enabledLabel=document.createElement("label"); enabledLabel.textContent="Enable recording ";
    const enabled=document.createElement("input"); enabled.type="checkbox"; enabled.checked=camera.recording_enabled;
    enabledLabel.append(enabled);
    const pre=document.createElement("input"); pre.type="number"; pre.min="0"; pre.max="120"; pre.value=camera.motion_pre_roll_seconds; pre.setAttribute("aria-label","Motion pre-roll seconds");
    const post=document.createElement("input"); post.type="number"; post.min="1"; post.max="600"; post.value=camera.motion_post_roll_seconds; post.setAttribute("aria-label","Motion post-roll seconds");
    const save=document.createElement("button"); save.type="button"; save.textContent="Apply recording policy";
    save.addEventListener("click", async () => {
      try {
        await api(`/api/cameras/${encodeURIComponent(camera.id)}`, { method:"PATCH", body:JSON.stringify({recording_mode:mode.value,recording_enabled:enabled.checked,motion_pre_roll_seconds:Number(pre.value),motion_post_roll_seconds:Number(post.value)}) });
        document.querySelector("#camera-message").textContent=`${camera.name}: recording mode updated.`;
        await refresh();
      } catch (error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const motionUrlLabel=document.createElement("label"); motionUrlLabel.textContent="ONVIF device service URL for motion events";
    const motionUrl=document.createElement("input"); motionUrl.type="url"; motionUrl.autocomplete="off"; motionUrl.placeholder="http://camera/onvif/device_service";
    const motionUserLabel=document.createElement("label"); motionUserLabel.textContent="ONVIF username";
    const motionUser=document.createElement("input"); motionUser.autocomplete="off";
    const motionPassLabel=document.createElement("label"); motionPassLabel.textContent="ONVIF password";
    const motionPass=document.createElement("input"); motionPass.type="password"; motionPass.autocomplete="new-password";
    const fallbackLabel=document.createElement("label"); fallbackLabel.textContent="Use server motion detection when ONVIF events are unavailable ";
    const fallback=document.createElement("input"); fallback.type="checkbox"; fallback.checked=camera.motion_fallback_enabled;
    fallbackLabel.append(fallback);
    const zonesLabel=document.createElement("label"); zonesLabel.textContent="Server motion zones (normalized rectangles; empty uses full frame)";
    const zones=document.createElement("textarea"); zones.spellcheck=false; zones.value=JSON.stringify(camera.motion_zones||[]); zones.placeholder='[{"x":0.1,"y":0.1,"width":0.8,"height":0.8}]';
    const sensitivityLabel=document.createElement("label"); sensitivityLabel.textContent="Server motion sensitivity (1–100)";
    const sensitivity=document.createElement("input"); sensitivity.type="number"; sensitivity.min="1"; sensitivity.max="100"; sensitivity.value=camera.motion_sensitivity||50;
    const configureMotion=document.createElement("button"); configureMotion.type="button"; configureMotion.textContent="Save motion event source";
    configureMotion.addEventListener("click",async()=>{
      try {
        const payload={pre_roll_seconds:Number(pre.value),post_roll_seconds:Number(post.value),fallback_enabled:fallback.checked,zones:JSON.parse(zones.value||"[]"),sensitivity:Number(sensitivity.value)};
        if(motionUrl.value) payload.service_url=motionUrl.value;
        if(motionUser.value||motionPass.value){payload.username=motionUser.value;payload.password=motionPass.value;}
        await api(`/api/cameras/${encodeURIComponent(camera.id)}/motion-source`,{method:"POST",body:JSON.stringify(payload)});
        motionUser.value=""; motionPass.value=""; await refresh();
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const events=document.createElement("button"); events.type="button"; events.textContent="View motion events";
    events.addEventListener("click",async()=>{
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/motion-events`);
        const target=document.querySelector("#segment-list");
        target.replaceChildren(...result.events.map(event=>{
          const row=document.createElement("p");
          row.textContent=formatDateTime(event.source_event_at)+" · "+event.source+" · "+event.status+" · pre-roll "+(event.pre_roll_complete ? "complete" : "incomplete/unknown")+(event.media_url ? " · playable clip available" : "");
          if(event.media_url) { const play=document.createElement("button"); play.type="button"; play.textContent="Play"; play.addEventListener("click",()=>{const player=document.querySelector("#playback");player.src=event.media_url;player.classList.remove("hidden");player.play().catch(()=>{});}); row.append(play); }
          return row;
        }));
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const aiEvents=document.createElement("button"); aiEvents.type="button"; aiEvents.textContent="Search person/vehicle events";
    aiEvents.addEventListener("click",async()=>{
      try {
        const label=document.createElement("select");
        for(const [value,text] of [["","All detections"],["person","Person"],["vehicle","Vehicle"]]){const option=document.createElement("option");option.value=value;option.textContent=text;label.append(option);}
        const from=document.createElement("input");from.type="datetime-local";from.setAttribute("aria-label","AI event start time");
        const to=document.createElement("input");to.type="datetime-local";to.setAttribute("aria-label","AI event end time");
        const run=document.createElement("button");run.type="button";run.textContent="Apply AI event search";
        const results=document.createElement("div");results.setAttribute("aria-live","polite");
        const search=async()=>{
          const params=new URLSearchParams({camera_id:camera.id});if(label.value)params.set("label",label.value);
          if(from.value)params.set("start",new Date(from.value).toISOString());if(to.value)params.set("end",new Date(to.value).toISOString());
          const found=await api(`/api/ai/events?${params}`);
          results.replaceChildren();
          const note=document.createElement("p");note.textContent=found.coverage_note;results.append(note);
          for(const item of found.events){const row=document.createElement("p");row.textContent=`${formatDateTime(item.first_seen_at)} · ${item.label} · ${(item.confidence*100).toFixed(1)}% · ${item.model_id} ${item.model_version} · OpenVINO ${item.provider}`;
            const play=document.createElement("button");play.type="button";play.textContent="Find playback";play.addEventListener("click",async()=>{
              const at=new Date(item.first_seen_at),begin=new Date(at.getTime()-30000),finish=new Date(at.getTime()+30000);
              const footage=await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments?from=${encodeURIComponent(begin.toISOString())}&to=${encodeURIComponent(finish.toISOString())}`);
              const segment=footage.segments.find(s=>new Date(s.starts_at)<=at&&new Date(s.ends_at)>=at)||footage.segments[0];
              if(!segment){row.append(document.createTextNode(" · no retained footage covers this observation"));return;}
              const player=document.querySelector("#playback");player.src=segment.media_url;player.classList.remove("hidden");
              player.addEventListener("loadedmetadata",()=>{const offset=Math.max(0,(at-new Date(segment.starts_at))/1000);player.currentTime=offset;},{once:true});player.play().catch(()=>{});
            });row.append(play);results.append(row);
          }
          if(!found.events.length){const empty=document.createElement("p");empty.textContent="No analyzed detections match this search. Check analysis coverage; empty results do not prove the scene was empty.";results.append(empty);}
        };
        run.addEventListener("click",async()=>{try{await search();}catch(error){results.textContent=error.message;}});
        await search();
        document.querySelector("#segment-list").replaceChildren(label,from,to,run,results);
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    let aiPanel=null;
    const aiConfigure=document.createElement("button");aiConfigure.type="button";aiConfigure.textContent="Configure person/vehicle detection";
    aiConfigure.addEventListener("click",async()=>{
      try{
        const config=await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai`);
        if(!aiPanel){aiPanel=document.createElement("section");card.append(aiPanel);}
        aiPanel.replaceChildren();const heading=document.createElement("h4");heading.textContent="Local CPU AI analysis";
        const availability=document.createElement("p");availability.textContent=`${config.model.id} ${config.model.version} · ${config.model.runtime} · ${config.model.available?"model ready":config.model.reason}`;
        const source=document.createElement("p");source.textContent=`Effective settings source: ${config.config_source}${config.config_source==="group"?" (inherited)":""}`;
        const inheritLabel=document.createElement("label");inheritLabel.textContent="Inherit assigned camera group AI settings ";const inherit=document.createElement("input");inherit.type="checkbox";inherit.checked=!config.config_override&&config.config_source==="group";inheritLabel.append(inherit);
        const enabledLabel=document.createElement("label");enabledLabel.textContent="Enable analysis ";const aiEnabled=document.createElement("input");aiEnabled.type="checkbox";aiEnabled.checked=config.enabled;enabledLabel.append(aiEnabled);
        const fpsLabel=document.createElement("label");fpsLabel.textContent="Analysis frames per second";const fps=document.createElement("input");fps.type="number";fps.min="0.1";fps.max="5";fps.step="0.1";fps.value=config.analysis_fps;fpsLabel.append(fps);
        const confidenceLabel=document.createElement("label");confidenceLabel.textContent="Minimum confidence (0.1–0.99)";const confidence=document.createElement("input");confidence.type="number";confidence.min="0.1";confidence.max="0.99";confidence.step="0.01";confidence.value=config.confidence_threshold;confidenceLabel.append(confidence);
        const substreamLabel=document.createElement("label");substreamLabel.textContent="Use validated lower-resolution stream ";const substream=document.createElement("input");substream.type="checkbox";substream.checked=config.use_substream;substreamLabel.append(substream);
        const zonesLabel=document.createElement("label");zonesLabel.textContent="AI zones (normalized rectangles; empty uses full frame)";const aiZones=document.createElement("textarea");aiZones.spellcheck=false;aiZones.value=JSON.stringify(config.zones);aiZones.placeholder='[{"x":0.1,"y":0.1,"width":0.8,"height":0.8}]';zonesLabel.append(aiZones);
        const saveAI=document.createElement("button");saveAI.type="button";saveAI.textContent="Save AI settings";
        const aiStatus=document.createElement("p");aiStatus.textContent=`Status: ${config.status}${config.last_success_at?` · last analyzed ${formatDateTime(config.last_success_at)}`:""}${config.last_error?` · ${config.last_error}`:""}`;
        saveAI.addEventListener("click",async()=>{try{const saved=await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai`,{method:"PUT",body:JSON.stringify({enabled:aiEnabled.checked,analysis_fps:Number(fps.value),confidence_threshold:Number(confidence.value),use_substream:substream.checked,zones:JSON.parse(aiZones.value||"[]"),inherit_group:inherit.checked})});aiStatus.textContent=`Status: ${saved.status} · settings ${saved.config_source}${saved.last_error?` · ${saved.last_error}`:""}`;}catch(error){aiStatus.textContent=error.message;}});
        const coverageButton=document.createElement("button");coverageButton.type="button";coverageButton.textContent="Refresh analysis coverage";coverageButton.addEventListener("click",async()=>{try{const status=await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai/coverage`);aiStatus.textContent=`Status: ${status.status} · ${status.intervals.length} analysis coverage intervals${status.last_error?` · ${status.last_error}`:""}`;}catch(error){aiStatus.textContent=error.message;}});
        aiPanel.append(heading,availability,source,inheritLabel,enabledLabel,fpsLabel,confidenceLabel,substreamLabel,zonesLabel,saveAI,coverageButton,aiStatus);
      }catch(error){document.querySelector("#camera-message").textContent=error.message;}
    });
    const coverage=document.createElement("button"); coverage.type="button"; coverage.textContent="View coverage history";
    const coverageList=document.createElement("ul");
    coverage.addEventListener("click",async()=>{
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/coverage`);
        coverageList.replaceChildren(...result.intervals.map(interval=>{
          const item=document.createElement("li");
          const end=interval.ends_at ? formatDateTime(interval.ends_at) : "ongoing";
          item.textContent=`${interval.coverage_kind}: ${interval.reason} · ${formatDateTime(interval.starts_at)} to ${end}`;
          return item;
        }));
        if(!result.intervals.length){const item=document.createElement("li");item.textContent="No coverage transitions recorded yet.";coverageList.replaceChildren(item);}
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const scheduleLabel=document.createElement("label"); scheduleLabel.textContent="Camera weekly recording schedule override (JSON; blank intervals mean scheduled idle)";
    const scheduleText=document.createElement("textarea"); scheduleText.spellcheck=false; scheduleText.placeholder='{"timezone":"America/Indiana/Indianapolis","intervals":[{"days":[0,1,2,3,4],"start":"08:00","end":"17:00","mode":"continuous"}]}';
    const inheritLabel=document.createElement("label"); inheritLabel.textContent="Inherit assigned group schedule ";
    const inheritSchedule=document.createElement("input"); inheritSchedule.type="checkbox"; inheritSchedule.checked=true; inheritLabel.append(inheritSchedule);
    const loadSchedule=document.createElement("button"); loadSchedule.type="button"; loadSchedule.textContent="Load camera schedule";
    loadSchedule.addEventListener("click",async()=>{
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/schedule`);
        inheritSchedule.checked=result.inherits_group;
        scheduleText.value=JSON.stringify({timezone:result.timezone,intervals:result.intervals},null,2);
        document.querySelector("#camera-message").textContent=`Loaded ${result.source} schedule${result.configured ? "" : " (no schedule configured)"}.`;
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const saveSchedule=document.createElement("button"); saveSchedule.type="button"; saveSchedule.textContent="Save camera schedule";
    saveSchedule.addEventListener("click",async()=>{
      try {
        const config=inheritSchedule.checked ? {} : JSON.parse(scheduleText.value||"{}");
        const payload=inheritSchedule.checked ? {inherit_group:true} : {timezone:config.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC",intervals:config.intervals||[]};
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/schedule`,{method:"PUT",body:JSON.stringify(payload)});
        document.querySelector("#camera-message").textContent=`${camera.name}: effective scheduled mode is ${result.effective_recording_mode}.`;
        await refresh();
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const remove=document.createElement("button"); remove.type="button"; remove.textContent="Remove camera";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Remove ${camera.name} and its saved footage?`)) return;
      try { await api(`/api/cameras/${encodeURIComponent(camera.id)}`, {method:"DELETE"}); await refresh(); }
      catch (error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const rotateLabel=document.createElement("label"); rotateLabel.textContent="Update stream credentials or URL";
    const rotateInput=document.createElement("input"); rotateInput.type="url"; rotateInput.autocomplete="off"; rotateInput.placeholder="rtsp://camera.example/stream";
    const rotate=document.createElement("button"); rotate.type="button"; rotate.textContent="Test and rotate stream";
    rotate.addEventListener("click",async()=>{
      try {
        await api(`/api/cameras/${encodeURIComponent(camera.id)}/credentials`,{method:"POST",body:JSON.stringify({stream_url:rotateInput.value})});
        document.querySelector("#bulk-message").textContent=`${camera.name}: updated stream validated and the recorder is reconnecting.`;
        rotateInput.value=""; await refresh();
      } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
    });
    card.append(mode,enabledLabel,pre,post,save,scheduleLabel,inheritLabel,scheduleText,loadSchedule,saveSchedule,motionUrlLabel,motionUrl,motionUserLabel,motionUser,motionPassLabel,motionPass,fallbackLabel,zonesLabel,zones,sensitivityLabel,sensitivity,configureMotion,events,aiEvents,aiConfigure,coverage,coverageList,remove,rotateLabel,rotateInput,rotate);
    return card;
  }));
}
async function loadSegments(camera) {
  const message = document.querySelector("#camera-message");
  const list = document.querySelector("#segment-list");
  const player = document.querySelector("#playback");
  message.textContent = `Loading indexed footage for ${camera.name}…`;
  list.replaceChildren(); player.pause(); player.removeAttribute("src"); player.classList.add("hidden");
  try {
    const result = await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments`);
    if (!result.segments.length) {
      message.textContent = result.recording_status === "reconnecting"
        ? "The camera stream is reconnecting. No indexed footage is available for playback yet."
        : result.reason;
      return;
    }
    message.textContent = `${result.segments.length} indexed interval(s) for ${camera.name}.`;
    list.replaceChildren(...result.segments.map(segment => {
      const item=document.createElement("li");
      const play=document.createElement("button"); play.type="button";
      play.textContent=`Play ${formatDateTime(segment.starts_at)} – ${formatDateTime(segment.ends_at).split(" ").slice(-1)[0]} · ${(segment.size_bytes/1048576).toFixed(1)} MiB`;
      play.addEventListener("click", () => { player.src=segment.media_url; player.classList.remove("hidden"); player.play().catch(() => { message.textContent="Interval loaded. Press Play to start playback."; }); });
      item.append(play);
      if(segment.preserved) {
        const held=document.createElement("span"); held.textContent=segment.hold_expires_at ? ` Preserved until ${formatDateTime(segment.hold_expires_at)}.` : " Preserved until an administrator releases the hold."; item.append(held);
        if(currentRole==="administrator") { const release=document.createElement("button"); release.type="button"; release.textContent="Release hold"; release.addEventListener("click",async()=>{try{await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments/${segment.id}/hold`,{method:"DELETE"});await loadSegments(camera);}catch(error){message.textContent=error.message;}}); item.append(release); }
      } else {
        const preserve=document.createElement("button"); preserve.type="button"; preserve.textContent="Preserve footage";
        let expiryInput=null;
        if(currentRole==="administrator"){expiryInput=document.createElement("input");expiryInput.type="number";expiryInput.min="1";expiryInput.max="3650";expiryInput.placeholder="No expiry";expiryInput.setAttribute("aria-label","Hold expiry in days");item.append(expiryInput);}
        preserve.addEventListener("click",async()=>{try{const payload={};if(expiryInput&&expiryInput.value)payload.expires_in_days=Number(expiryInput.value);await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments/${segment.id}/hold`,{method:"POST",body:JSON.stringify(payload)});await loadSegments(camera);}catch(error){message.textContent=error.message;}}); item.append(preserve);
      }
      if(currentRole==="administrator") { const remove=document.createElement("button"); remove.type="button"; remove.textContent="Delete footage"; remove.disabled=segment.preserved; remove.addEventListener("click",async()=>{try{await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments/${segment.id}`,{method:"DELETE"});await loadSegments(camera);}catch(error){message.textContent=error.message;}});item.append(remove); }
      return item;
    }));
  } catch (error) { message.textContent=error.message; }
}
document.querySelector("#load-camera-profiles").addEventListener("click", async event => {
  const button=event.currentTarget; button.disabled=true;
  try { await loadCameraProfiles(); }
  catch(error) { document.querySelector("#camera-profile-message").textContent=error.message; }
  finally { button.disabled=false; }
});
document.querySelector("#test-camera").addEventListener("click", async () => {
  const message=document.querySelector("#camera-message"); message.textContent="Testing camera video…";
  try {
    const source=cameraSourcePayload();
    const result=await api("/api/cameras/test", { method:"POST", body:JSON.stringify({...source,motion_service_url:document.querySelector("#motion-service-url").value||null}) });
    message.textContent=result.reachable ? `Video available · ${result.video.codec} ${result.video.width || "?"}×${result.video.height || "?"}` : result.reason;
  } catch (error) { message.textContent=error.message; }
});
document.querySelector("#discover-cameras").addEventListener("click", async () => {
  const message=document.querySelector("#discovery-message");
  const button=document.querySelector("#discover-cameras");
  button.disabled=true; message.textContent="Scanning the camera VLAN for ONVIF devices…";
  try {
    const result=await api("/api/discovery/scan",{method:"POST",body:JSON.stringify({timeout_seconds:Number(document.querySelector("#discovery-timeout").value)})});
    message.textContent=`Found ${result.discovered_count} device(s). ${result.partial_failures.join(" ")}`;
    await refreshDiscovery();
  } catch(error) { message.textContent=error.message; }
  finally { button.disabled=false; }
});
document.querySelector("#discover-ip-range").addEventListener("click", async event => {
  const button=event.currentTarget, message=document.querySelector("#discovery-message");
  const ip_range=document.querySelector("#discovery-ip-range").value.trim();
  if(!ip_range) { message.textContent="Enter a private IPv4 CIDR range, such as 192.0.2.0/24."; return; }
  button.disabled=true; message.textContent="Scanning the camera range with unicast ONVIF probes…";
  try {
    const result=await api("/api/discovery/scan",{method:"POST",body:JSON.stringify({ip_range,timeout_seconds:Number(document.querySelector("#discovery-timeout").value)})});
    message.textContent=`Checked ${result.targets_scanned} address(es); found ${result.discovered_count} ONVIF device(s).`;
    await refreshDiscovery();
  } catch(error) { message.textContent=error.message; }
  finally { button.disabled=false; }
});
document.querySelector("#diagnostics-toggle").addEventListener("click", async () => {
  const button=document.querySelector("#diagnostics-toggle"); button.disabled=true;
  try {
    const enabled=button.textContent.startsWith("Enable");
    await api("/api/diagnostics/capture",{method:"POST",body:JSON.stringify({enabled,hours:Number(document.querySelector("#diagnostics-hours").value)})});
    await refreshDiagnostics();
  } catch(error) { document.querySelector("#diagnostics-message").textContent=error.message; }
  finally { button.disabled=false; }
});
document.querySelector("#diagnostics-download").addEventListener("click", async () => {
  try {
    const response=await fetch("/api/diagnostics/bundle",{credentials:"same-origin"});
    if(!response.ok) throw new Error("Could not download diagnostics bundle");
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement("a"); link.href=url; link.download="nvr-diagnostics.json"; link.click(); URL.revokeObjectURL(url);
  } catch(error) { document.querySelector("#diagnostics-message").textContent=error.message; }
});
document.querySelector("#group-form").addEventListener("submit",async event=>{
  event.preventDefault();
  try {
    const result=await api("/api/camera-groups",{method:"POST",body:JSON.stringify({name:document.querySelector("#new-group-name").value})});
    document.querySelector("#bulk-message").textContent=`Created camera group ${result.name}.`;
    event.currentTarget.reset(); await refreshGroups();
  } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
});
document.querySelector("#load-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  if(!group) { document.querySelector("#schedule-message").textContent="Create or select a camera group first."; return; }
  try {
    const schedule=await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`);
    document.querySelector("#group-schedule-json").value=JSON.stringify({timezone:schedule.timezone,intervals:schedule.intervals},null,2);
    document.querySelector("#schedule-message").textContent=schedule.configured ? "Group schedule loaded." : "No group schedule is configured.";
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#save-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try {
    if(!group) throw new Error("Create or select a camera group first.");
    const schedule=JSON.parse(document.querySelector("#group-schedule-json").value||"{}");
    const result=await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`,{method:"PUT",body:JSON.stringify({timezone:schedule.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC",intervals:schedule.intervals||[]})});
    document.querySelector("#schedule-message").textContent=`Saved ${result.intervals.length} group schedule interval(s).`;
    await refresh();
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#clear-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try {
    if(!group) throw new Error("Create or select a camera group first.");
    await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`,{method:"DELETE"});
    document.querySelector("#schedule-message").textContent="Group schedule removed; assigned cameras now use their camera policy or camera-level override.";
    await refresh();
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#load-group-retention").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  if(!group) { document.querySelector("#schedule-message").textContent="Create or select a camera group first."; return; }
  try { const policy=await api(`/api/camera-groups/${encodeURIComponent(group)}/retention`); document.querySelector("#group-retention-days").value=policy.retention_days; document.querySelector("#schedule-message").textContent=policy.configured ? "Group retention loaded." : "Group uses the 30 day default."; }
  catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#save-group-retention").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try { if(!group) throw new Error("Create or select a camera group first."); const retention_days=Number(document.querySelector("#group-retention-days").value); const policy=await api(`/api/camera-groups/${encodeURIComponent(group)}/retention`,{method:"PUT",body:JSON.stringify({retention_days})}); document.querySelector("#schedule-message").textContent=`Saved group retention target: ${policy.retention_days} days.`; await refresh(); }
  catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#clear-group-retention").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try { if(!group) throw new Error("Create or select a camera group first."); await api(`/api/camera-groups/${encodeURIComponent(group)}/retention`,{method:"DELETE"}); document.querySelector("#schedule-message").textContent="Group retention removed; cameras use their camera override or the 30 day default."; await refresh(); }
  catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#load-storage-volumes").addEventListener("click",async()=>{
  try {
    const group=document.querySelector("#schedule-group").value;
    const [status,assignment]=await Promise.all([api("/api/storage"),group?api(`/api/camera-groups/${encodeURIComponent(group)}/storage-volume`):Promise.resolve({volume_id:"default"})]);
    const select=document.querySelector("#group-storage-volume");
    select.replaceChildren(...status.volumes.map(volume=>new Option(`${volume.id} · ${volume.state} · ${volume.free_bytes==null?"capacity unknown":Math.round(volume.free_bytes/1048576)+" MiB free"}`,volume.id)));
    select.value=assignment.volume_id;
    document.querySelector("#storage-volume-status").replaceChildren(...status.volumes.map(volume=>{const item=document.createElement("li");item.textContent=`${volume.id}: ${volume.state}; ${volume.cameras.length} assigned camera(s)${volume.retention_at_risk?" · retention at risk":""}`;return item;}));
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#save-storage-volume").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try {if(!group) throw new Error("Create or select a camera group first.");const result=await api(`/api/camera-groups/${encodeURIComponent(group)}/storage-volume`,{method:"PUT",body:JSON.stringify({volume_id:document.querySelector("#group-storage-volume").value})});document.querySelector("#schedule-message").textContent=`Assigned group cameras to ${result.volume_id}.`;await refresh();}
  catch(error){document.querySelector("#schedule-message").textContent=error.message;}
});
document.querySelector("#apply-bulk").addEventListener("click",async()=>{
  const camera_ids=[...document.querySelectorAll("[data-camera-select]:checked")].map(input=>input.value);
  const payload={camera_ids};
  const group=document.querySelector("#bulk-group").value;
  const mode=document.querySelector("#bulk-recording-mode").value;
  const recordingEnabled=document.querySelector("#bulk-recording-enabled").value;
  if(group!=="__unchanged__") payload.group_name=group || null;
  if(mode) payload.recording_mode=mode;
  if(recordingEnabled) payload.recording_enabled=recordingEnabled==="true";
  try {
    const result=await api("/api/cameras/bulk-update",{method:"POST",body:JSON.stringify(payload)});
    document.querySelector("#bulk-message").textContent=`Updated ${result.cameras.length} camera(s).`;
    await refresh();
  } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
});
document.querySelector("#preview-import").addEventListener("click",async()=>{
  const message=document.querySelector("#bulk-message");
  const csv_text=document.querySelector("#camera-import-csv").value;
  importCanApply=false; lastPreviewCsv=""; document.querySelector("#apply-import").disabled=true;
  try {
    const result=await api("/api/cameras/import/preview",{method:"POST",body:JSON.stringify({csv_text})});
    const target=document.querySelector("#import-preview");
    target.replaceChildren(...result.rows.map(row=>{
      const line=document.createElement("p");
      line.textContent=`CSV row ${row.row} · ${row.name || "Unnamed"} · ${row.group_name || "No group"} · ${row.status}${row.errors.length ? ` · ${row.errors.join("; ")}` : ""}`;
      return line;
    }));
    message.textContent=`Preview: ${result.accepted_count} accepted, ${result.rejected_count} rejected. Stream credentials are not echoed.`;
    importCanApply=result.accepted_count>0; lastPreviewCsv=csv_text; document.querySelector("#apply-import").disabled=!importCanApply;
  } catch(error) { message.textContent=error.message; }
});
document.querySelector("#apply-import").addEventListener("click",async()=>{
  if(!importCanApply || lastPreviewCsv!==document.querySelector("#camera-import-csv").value) {
    document.querySelector("#bulk-message").textContent="Preview the current CSV again before importing."; return;
  }
  const button=document.querySelector("#apply-import"); button.disabled=true;
  try {
    const result=await api("/api/cameras/import/apply",{method:"POST",body:JSON.stringify({csv_text:lastPreviewCsv})});
    const target=document.querySelector("#import-preview");
    target.replaceChildren(...result.rows.map(row=>{ const line=document.createElement("p"); line.textContent=`CSV row ${row.row} · ${row.name || "Unnamed"} · ${row.status}${row.errors.length ? ` · ${row.errors.join("; ")}` : ""}`; return line; }));
    document.querySelector("#bulk-message").textContent=`Imported ${result.created_count} camera(s); ${result.failed_count} row(s) need attention.`;
    importCanApply=false; await refresh();
  } catch(error) { document.querySelector("#bulk-message").textContent=error.message; button.disabled=false; }
});
document.querySelector("#load-group-ai").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try{
    if(!group)throw new Error("Create or select a camera group first.");
    const config=await api(`/api/camera-groups/${encodeURIComponent(group)}/ai`);
    document.querySelector("#group-ai-enabled").checked=config.enabled;
    document.querySelector("#group-ai-fps").value=config.analysis_fps;
    document.querySelector("#group-ai-confidence").value=config.confidence_threshold;
    document.querySelector("#group-ai-substream").checked=config.use_substream;
    document.querySelector("#group-ai-zones").value=JSON.stringify(config.zones);
    document.querySelector("#ai-group-message").textContent=`${config.group_name}: ${config.inherited_camera_count}/${config.camera_count} camera(s) inherit these settings · ${config.model.available?"model ready":config.model.reason}.`;
  }catch(error){document.querySelector("#ai-group-message").textContent=error.message;}
});
async function loadAiRuntimeSettings() {
  const result=await api("/api/ai/settings");
  const model=result.model;
  const providerSelect=document.querySelector("#ai-provider");
  providerSelect.replaceChildren(...model.provider_options.map(item=>new Option(`${item.id} · ${item.available?"available":"unavailable"}${item.reason?` — ${item.reason}`:""}`,item.id)));
  providerSelect.value=model.provider;
  const models=document.querySelector("#ai-model");
  models.replaceChildren(...model.model_options.map(item=>new Option(`${item.id} · ${item.version} · ${item.source}${item.available?"":" · files unavailable"}`,item.id)));
  models.value=model.model_id;
  document.querySelector("#ai-max-active").value=result.max_active_cameras;
  document.querySelector("#ai-runtime-state").textContent=`Active: ${model.runtime} · ${model.model_id} ${model.model_version} · ${model.available?"available":"unavailable: "+model.reason} · ${model.validated?"validated "+model.validated_at:"not validated on this host"}. Model replacement requires an NVR-compatible 448×256 boxes/labels output contract.`;
}
document.querySelector("#load-ai-runtime").addEventListener("click",async()=>{
  try{await loadAiRuntimeSettings();document.querySelector("#ai-group-message").textContent="AI provider and compatible model options refreshed.";}
  catch(error){document.querySelector("#ai-group-message").textContent=error.message;}
});
document.querySelector("#validate-ai-runtime").addEventListener("click",async()=>{
  try{
    const result=await api("/api/ai/settings/validate",{method:"POST",body:JSON.stringify({provider:document.querySelector("#ai-provider").value,model_id:document.querySelector("#ai-model").value})});
    document.querySelector("#ai-runtime-state").textContent=result.valid?`${result.runtime} ${result.model_id} ${result.model_version} passed model compile and test inference; saving will validate again before activation. ${result.gpu_certification_note}`:`Validation failed for ${result.provider}/${result.model_id}: ${result.reason}`;
  }catch(error){document.querySelector("#ai-runtime-state").textContent=error.message;}
});
document.querySelector("#save-group-ai").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try{
    if(!group)throw new Error("Create or select a camera group first.");
    const result=await api(`/api/camera-groups/${encodeURIComponent(group)}/ai`,{method:"PUT",body:JSON.stringify({enabled:document.querySelector("#group-ai-enabled").checked,analysis_fps:Number(document.querySelector("#group-ai-fps").value),confidence_threshold:Number(document.querySelector("#group-ai-confidence").value),use_substream:document.querySelector("#group-ai-substream").checked,zones:JSON.parse(document.querySelector("#group-ai-zones").value||"[]")})});
    document.querySelector("#ai-group-message").textContent=`Saved group settings for ${result.inherited_camera_count} inherited camera(s). Cameras beyond the configured AI limit will show skipped analysis coverage.`;
  }catch(error){document.querySelector("#ai-group-message").textContent=error.message;}
});
document.querySelector("#save-ai-runtime").addEventListener("click",async()=>{
  try{
    const result=await api("/api/ai/settings",{method:"PUT",body:JSON.stringify({max_active_cameras:Number(document.querySelector("#ai-max-active").value),provider:document.querySelector("#ai-provider").value,model_id:document.querySelector("#ai-model").value})});
    document.querySelector("#ai-group-message").textContent=`${result.model.runtime} with ${result.model.model_id} ${result.model.model_version} validated and activated for ${result.max_active_cameras} active camera(s). Inference remains serialized in one bounded slot to prioritize recording.`;
    await loadAiRuntimeSettings();
  }catch(error){document.querySelector("#ai-group-message").textContent=error.message;}
});
document.querySelector("#camera-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message=document.querySelector("#camera-message"); message.textContent="Testing camera and adding it…";
  try {
    const source=cameraSourcePayload();
    const result=await api("/api/cameras", { method:"POST", body:JSON.stringify({
      name:document.querySelector("#camera-name").value,
      ...source,
      recording_mode:document.querySelector("#recording-mode").value,
      recording_enabled:document.querySelector("#recording-enabled").checked,
      motion_service_url:document.querySelector("#motion-service-url").value || null,
      motion_username:document.querySelector("#motion-username").value || null,
      motion_password:document.querySelector("#motion-password").value || null,
      motion_pre_roll_seconds:Number(document.querySelector("#motion-pre-roll").value),
      motion_post_roll_seconds:Number(document.querySelector("#motion-post-roll").value),
      motion_fallback_enabled:document.querySelector("#motion-fallback-enabled").checked,
      motion_zones:JSON.parse(document.querySelector("#motion-zones").value||"[]"),
      motion_sensitivity:Number(document.querySelector("#motion-sensitivity").value)
    }) });
    document.querySelector("#camera-form").reset();
    document.querySelector("#camera-profile").replaceChildren(new Option("Load endpoints to choose a stream",""));
    document.querySelector("#camera-live-profile").replaceChildren(new Option("Use main stream for grid",""));
    message.textContent=`${result.name} added · ${result.video.codec} ${result.video.width || "?"}×${result.video.height || "?"}`;
    await refresh();
  } catch (error) { message.textContent=error.message; }
});
document.querySelector("#setup-form").addEventListener("submit", async event => {
  event.preventDefault(); message.textContent = "";
  try { await api("/api/setup/admin", { method:"POST", body:JSON.stringify({ username:document.querySelector("#setup-user").value, password:document.querySelector("#setup-pass").value }) }); await refresh(); }
  catch (error) { message.textContent = error.message; }
});
document.querySelector("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); message.textContent = "";
  try { await api("/api/auth/login", { method:"POST", body:JSON.stringify({ username:document.querySelector("#login-user").value, password:document.querySelector("#login-pass").value }) }); await refresh(); }
  catch (error) { message.textContent = error.message; }
});
document.querySelector("#live-start").addEventListener("click",()=>startLiveViews(selectedLiveCameras(),latestCameras));
document.querySelector("#live-camera-picker").addEventListener("change",event=>{if(event.target.matches("[data-live-camera]"))markLiveViewDirty();});
document.querySelector("#live-grid-size").addEventListener("change",event=>{
  const size=Number(event.target.value);
  if(activeLiveCameraIds.length>size){document.querySelector("#live-message").textContent=`The active grid has ${activeLiveCameraIds.length} cameras. Remove cameras before switching to ${size} feeds.`;event.target.value=String(activeLiveGridSize);return;}
  if(activeLiveCameraIds.length)startLiveViews(activeLiveCameraIds,latestCameras,null,size);
  else markLiveViewDirty();
});
document.querySelector("#live-stop").addEventListener("click",()=>{stopLiveViews();cachedLiveArrangement=null;document.querySelector("#live-message").textContent="Live viewing stopped.";});
document.querySelector("#live-save-layout").addEventListener("click",async()=>{
  const name=document.querySelector("#live-layout-name").value.trim(), scope=document.querySelector("#live-layout-scope").value;
  const size=Number(document.querySelector("#live-grid-size").value)||4;
  let tiles;
  const selected=new Set(selectedLiveCameras());
  if(selected.size>size){document.querySelector("#live-message").textContent=`The ${size}-feed grid cannot save ${selected.size} selected cameras. Choose a larger grid or remove cameras.`;return;}
  if(activeLiveTileCameraIds.length===size){tiles=currentLiveTiles(size).map(id=>id&&selected.has(id)?id:null);for(const id of selected)if(!tiles.includes(id)){const slot=tiles.indexOf(null);if(slot>=0)tiles[slot]=id;}}
  else tiles=[...selected,...Array(size-selected.size).fill(null)];
  const camera_ids=tiles.filter(Boolean);
  try {if(!name)throw new Error("Enter a view name.");if(!camera_ids.length)throw new Error("Select at least one camera.");
    const path=scope==="shared"?"shared-layouts":"layouts";
    const result=await api(`/api/live/${path}/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({camera_ids,tile_camera_ids:tiles,grid_size:size})});
    clearLiveViewDirty();document.querySelector("#live-message").textContent=`Saved ${scope} view “${result.name}”.`;await refreshLiveLayouts();document.querySelector("#live-layouts").value=`${scope}:${result.name}`;}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
document.querySelector("#live-load-layout").addEventListener("click",async()=>{
  const key=document.querySelector("#live-layouts").value;
  try {if(!key)throw new Error("Choose a saved view.");const layout=sidebarViews.find(item=>`${item.scope}:${item.name}`===key);if(!layout)throw new Error("Saved view is unavailable.");applySavedView(layout);}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
document.querySelector("#live-delete-layout").addEventListener("click",async()=>{
  const key=document.querySelector("#live-layouts").value;
  try {if(!key)throw new Error("Choose a saved view.");const [scope,name]=key.split(":",2);if(scope==="shared"&&currentRole!=="administrator")throw new Error("Only administrators can delete shared views.");await api(`/api/live/${scope==="shared"?"shared-layouts":"layouts"}/${encodeURIComponent(name)}`,{method:"DELETE"});document.querySelector("#live-message").textContent=`Deleted “${name}”.`;await refreshLiveLayouts();}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
window.addEventListener("pagehide",stopLiveViews);
window.addEventListener("keydown",event=>{
  if(event.key==="Escape"&&focusedLiveCameraId){event.preventDefault();startLiveViews(activeLiveCameraIds,latestCameras,null,activeLiveGridSize,[...activeLiveTileCameraIds]);}
});
document.querySelector("#playback-search").addEventListener("click",loadSynchronizedPlayback);
document.querySelector("#playback-grid-size").addEventListener("change",event=>{
  const size=Number(event.target.value);
  document.querySelector("#playback-grid").style.setProperty("--playback-grid-columns",String(Math.ceil(Math.sqrt(size))));
});
document.querySelector("#playback-stop").addEventListener("click",()=>{stopSynchronizedPlayback();document.querySelector("#playback-status").textContent="Playback stopped.";});
document.querySelector("#playback-export").addEventListener("click",requestPlaybackExport);
document.querySelector("#playback-cursor").addEventListener("input",event=>{
  if(!playbackTimeline)return;
  const start=playbackMilliseconds(playbackTimeline.start),end=playbackMilliseconds(playbackTimeline.end);
  const target=start+(end-start)*Number(event.target.value)/1000;
  const playing=playbackViews.some(view=>!view.video.paused);
  seekSynchronizedPlayback(target,playing);
});
document.querySelector("#playback-rate").addEventListener("change",event=>{playbackViews.forEach(view=>{view.video.playbackRate=Number(event.target.value);});});
window.addEventListener("pagehide",stopSynchronizedPlayback);
window.addEventListener("pagehide",()=>{if(exportPollTimer)clearInterval(exportPollTimer);exportPollTimer=null;});
if(incidentPollTimer) clearInterval(incidentPollTimer);
incidentPollTimer=setInterval(()=>{if(currentRole)refreshIncidents();},15000);
if(webhookPollTimer) clearInterval(webhookPollTimer);
webhookPollTimer=setInterval(()=>{if(currentRole==="administrator"){refreshWebhooks();refreshHeartbeatStatus();}},10000);
document.querySelector("#logout").addEventListener("click", async () => { stopLiveViews(); cachedLiveArrangement=null;workspaceRestored=false;await api("/api/auth/logout", { method:"POST" }); currentRole=null; document.querySelector("#incidents-list").replaceChildren(); document.querySelector("#webhook-list").replaceChildren(); document.querySelector("#webhook-deliveries").replaceChildren(); show("login"); });
document.querySelector("#entra-login").addEventListener("click", async () => {
  const button=document.querySelector("#entra-login"); button.disabled=true; message.textContent="Starting Microsoft Entra sign-in…";
  try {
    const response=await fetch("/api/auth/oidc/login?format=json", {credentials:"same-origin"});
    const result=await response.json();
    if (!response.ok) throw new Error(result.detail || "Entra sign-in is unavailable");
    window.location.assign(result.authorization_url);
  } catch (error) {
    message.textContent=`${error.message}. You can still sign in with the emergency local administrator account.`;
    button.disabled=false;
  }
});
refresh();
