const sections = ["setup", "login", "status"];
const message = document.querySelector("#message");
let currentRole = null;
let importCanApply = false;
let lastPreviewCsv = "";
let liveViews = [];
let liveStatusTimer = null;
const LIVE_GRID_SIZES = [1, 4, 9, 16];
const {autoLayoutForCount, reconcileCameraOrder} = window.NvrWorkspaceLayout;
let activeLiveCameraIds = [];
let activeLiveTileCameraIds = [];
let activeLiveGridSize = 4;
let activeLiveLayoutMode = "auto";
let activeLiveTileCameraId = null;
let focusedLiveCameraId = null;
let latestCameras = [];
let latestSystemStatus = null;
let latestStorageStatus = null;
let latestExportProblemCount = 0;
let latestActiveIncidentCount = 0;
let operationalStatusError = false;
let latestOperationalRefreshAt = null;
let currentUsername = "";
let playbackWorkspaceRestored = false;
let pendingPlaybackRestoreState = null;
let playbackWorkspaceSaveTimer = null;
let playbackTimeline = null;
let playbackViews = [];
let playbackMaster = null;
let playbackSyncGuard = false;
let playbackWindow = null;
let playbackManualChoices = new Map();
let playbackResultsStale = false;
let playbackClockTimer = null;
let playbackClockAnchor = null;
let playbackIsPlaying = false;
let playbackExpandedView = null;
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
let selectedCameraId = null;
const cameraEditorState=window.NvrCameraEditor.createCameraEditorState();
const cameraPreviewWorkspace=window.NvrCameraPreviewWorkspace.createCameraPreviewWorkspace();
let cameraEditorSaveInProgress=false;
let cameraDirtyDecisionDialog=null;
let cameraDirtyDecisionResolve=null;
let cameraDirtyDecisionPromise=null;
const cameraBulkSelection = new Set();
let activeCameraWorkspace = "cameras";
let mobileCameraEditorOpen = false;
const cameraInventoryFilters = { archive:"active", group:"all", connection:"all", health:"all", sortKey:"name", sortDirection:1 };
let displaySettings = {time_zone:"UTC",date_format:"friendly",clock_format:"12"};
let currentBranding = {app_name:"Workplace NVR",primary_color:"#17212b",accent_color:"#69a1ff",assets:{}};
let brandingPreviewUrl = null;
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
function updateExportFoldChoices(field) {
  const input=document.querySelector(`#export-${field}`),select=document.querySelector(`#export-${field}-fold`),candidates=localTimeCandidates(input.value);
  select.replaceChildren(new Option("Choose occurrence",""));
  if(candidates.length>1){for(const [index,instant] of candidates.entries()){const zoneOffset=new Intl.DateTimeFormat("en-US",{timeZone:displaySettings.time_zone,timeZoneName:"shortOffset"}).formatToParts(new Date(instant)).find(part=>part.type==="timeZoneName")?.value||`Occurrence ${index+1}`;select.add(new Option(`${index===0?"First":"Second"} occurrence · ${zoneOffset}`,String(instant)));}select.classList.remove("hidden");select.disabled=false;}
  else{select.classList.add("hidden");select.disabled=true;}
}
function resolveExportLocalTime(field) {
  const input=document.querySelector(`#export-${field}`),fold=document.querySelector(`#export-${field}-fold`),candidates=localTimeCandidates(input.value);
  if(!input.value)throw new Error("Choose an export start and end time.");
  if(!candidates.length)throw new Error(`The export ${field} time does not exist in ${displaySettings.time_zone}.`);
  if(candidates.length>1){const chosen=Number(fold.value);if(!candidates.includes(chosen))throw new Error(`Choose which repeated export ${field} time to use.`);return chosen;}
  return candidates[0];
}
function storedLocalInstant(value,foldValue) {
  const candidates=localTimeCandidates(value||"");
  if(candidates.length===1)return candidates[0];
  const chosen=Number(foldValue);
  return candidates.includes(chosen)?chosen:null;
}
function selectedExportRange() {
  if(!playbackTimeline)throw new Error("Search footage before preparing an export.");
  if(playbackResultsStale)throw new Error("Search inputs changed. Load the timeline again before exporting.");
  const start=resolveExportLocalTime("start"),end=resolveExportLocalTime("end"),boundStart=playbackMilliseconds(playbackTimeline.start),boundEnd=playbackMilliseconds(playbackTimeline.end);
  if(end<=start)throw new Error("Export end must be later than export start.");
  if(start<boundStart||end>boundEnd)throw new Error("Export marks must stay within the executed search range.");
  if(end-start>24*60*60*1000)throw new Error("Export ranges are limited to 24 hours.");
  return {start,end};
}
function updatePlaybackExportPreview() {
  const message=document.querySelector("#export-preflight"),button=document.querySelector("#playback-export"),enabled=Boolean(playbackTimeline&&!playbackResultsStale);
  ["#playback-mark-in","#playback-mark-out","#playback-clear-marks","#export-start","#export-end"].forEach(selector=>{document.querySelector(selector).disabled=!enabled;});
  button.disabled=!enabled;
  document.querySelectorAll("[data-export-camera]").forEach(input=>{input.disabled=!enabled;});
  const selectedViews=playbackViews.filter(view=>[...document.querySelectorAll("[data-export-camera]:checked")].some(input=>input.value===view.cameraId));
  const audioToggle=document.querySelector("#export-include-audio"),sourceSelect=document.querySelector("#export-grid-audio-source");
  const gridSelected=[...document.querySelectorAll('[data-export-mode="grid"]')].some(input=>input.checked);
  const selectableAudio=selectedViews.filter(view=>view.audio_supported===true&&view.audio_enabled);
  audioToggle.disabled=!enabled||!selectableAudio.length;
  if(audioToggle.disabled)audioToggle.checked=false;
  sourceSelect.replaceChildren(...selectableAudio.map(view=>{const option=document.createElement("option");option.value=view.cameraId;option.textContent=view.name;return option;}));
  sourceSelect.disabled=!enabled||!audioToggle.checked||!gridSelected||!selectableAudio.length;
  if(!playbackTimeline){message.textContent="Search footage to preview recording coverage and gaps.";renderPlaybackTracks();return;}
  if(playbackResultsStale){message.textContent="Search inputs changed; export is unavailable until the timeline is reloaded.";renderPlaybackTracks();return;}
  try {
    const range=selectedExportRange(),cameraIds=[...document.querySelectorAll("[data-export-camera]:checked")].map(input=>input.value),eligible=[],omitted=[],gaps=[];
    for(const view of playbackViews.filter(item=>cameraIds.includes(item.cameraId))){
      const seconds=view.segments.reduce((sum,segment)=>sum+Math.max(0,Math.min(range.end,playbackMilliseconds(segment.ends_at))-Math.max(range.start,playbackMilliseconds(segment.starts_at)))/1000,0);
      const gapSeconds=view.gaps.reduce((sum,gap)=>sum+Math.max(0,Math.min(range.end,playbackMilliseconds(gap.ends_at))-Math.max(range.start,playbackMilliseconds(gap.starts_at)))/1000,0);
      if(seconds>0)eligible.push(view);else omitted.push(view.name);
      gaps.push(`${view.name}: ${gapSeconds.toFixed(0)}s gap`);
    }
    if(!cameraIds.length)throw new Error("Select at least one searched camera for export.");
    if(!eligible.length){button.disabled=true;message.textContent=`No footage overlaps this export range. ${omitted.length?`No-footage cameras: ${omitted.join(", ")}.`:""}`;renderPlaybackTracks();return;}
    message.textContent=`${formatDateTime(range.start)} – ${formatDateTime(range.end)} · footage from ${eligible.map(view=>view.name).join(", ")}${omitted.length?` · omitted without footage: ${omitted.join(", ")}`:""}${gaps.length?` · ${gaps.join("; ")}`:""}`;
    renderPlaybackTracks();
  }catch(error){button.disabled=true;message.textContent=error.message;renderPlaybackTracks();}
}
function initializeExportSelection(selectedIds=null) {
  const target=document.querySelector("#export-camera-selection"),legend=target.querySelector("legend");
  target.replaceChildren(legend);
  if(!playbackViews.length){const note=document.createElement("p");note.textContent="Search footage to choose cameras.";target.append(note);return;}
  const selected=selectedIds?new Set(selectedIds):null;
  for(const view of playbackViews){const label=document.createElement("label"),input=document.createElement("input");input.type="checkbox";input.checked=selected?selected.has(view.cameraId):true;input.value=view.cameraId;input.dataset.exportCamera="";input.addEventListener("change",updatePlaybackExportPreview);label.append(input,document.createTextNode(` ${view.name}`));target.append(label);}
}
function setExportBoundary(field,instant) {
  document.querySelector(`#export-${field}`).value=localInputForInstant(new Date(instant));
  updateExportFoldChoices(field);
  updatePlaybackExportPreview();
  savePlaybackWorkspace();
}
function updateExportSelectionOverlay(track) {
  track.querySelector(".playback-export-selection")?.remove();
  try {
    const {start,end}=selectedExportRange(),window=currentPlaybackWindow();if(!window||end<=window.start||start>=window.end)return;
    const overlay=document.createElement("span");overlay.className="playback-export-selection";overlay.style.left=`${Math.max(0,(start-window.start)/(window.end-window.start)*100)}%`;overlay.style.width=`${Math.max(0,Math.min(100,(end-window.start)/(window.end-window.start)*100)-Math.max(0,(start-window.start)/(window.end-window.start)*100))}%`;overlay.setAttribute("aria-label",`Selected export range ${formatDateTime(start)} to ${formatDateTime(end)}`);track.append(overlay);
  }catch{}
}
function show(name) { if(name!=="status") stopSynchronizedPlayback(); document.querySelector("main > h1").classList.toggle("hidden", name === "status"); for (const id of sections) document.querySelector(`#${id}`).classList.toggle("hidden", id !== name); }
function moveInto(selector, targetSelector) {
  const element = document.querySelector(selector), target = document.querySelector(targetSelector);
  if (element && target) target.append(element);
}
function cameraWorkspace(id, title) {
  const section=document.createElement("section");
  section.className="camera-admin-workspace";
  section.dataset.cameraWorkspace=id;
  section.setAttribute("aria-label",title);
  section.classList.toggle("hidden",id!==activeCameraWorkspace);
  return section;
}
async function setCameraWorkspace(id) {
  const target=document.querySelector(`[data-camera-workspace="${id}"]`);
  if(!target)return false;
  const current=document.querySelector(`[data-camera-workspace="${activeCameraWorkspace}"]`);
  if(current&&current!==target){
    const allowed=activeCameraWorkspace==="cameras"?await guardCameraDrafts():guardDraftsIn(current);
    if(!allowed)return false;
  }
  if(activeCameraWorkspace==="cameras"&&id!=="cameras")cameraPreviewWorkspace.leave();
  activeCameraWorkspace=id;
  document.querySelectorAll("[data-camera-workspace]").forEach(section=>section.classList.toggle("hidden",section!==target));
  document.querySelectorAll("[data-camera-workspace-button]").forEach(button=>{
    if(button.dataset.cameraWorkspaceButton===id)button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  });
  mobileCameraEditorOpen=false;
  const layout=document.querySelector("#camera-admin-layout");if(layout)layout.dataset.mobileEditor="false";
  return true;
}
function setupCameraAdminWorkspaces() {
  const content=document.querySelector("#camera-settings-content");
  if(content.querySelector("#camera-workspace-nav"))return;

  const heading=document.querySelector("#camera-heading");
  const searchLabel=document.querySelector('label[for="camera-inventory-search"]');
  const search=document.querySelector("#camera-inventory-search");
  const editor=document.querySelector("#camera-list");
  const message=document.querySelector("#camera-message");
  const segmentsHeading=[...document.querySelectorAll("h3")].find(item=>item.textContent.trim()==="Recorded footage");
  const segments=document.querySelector("#segment-list");
  const player=document.querySelector("#playback");
  const form=document.querySelector("#camera-form");
  const discovery=document.querySelector("#discovery-admin");
  const groups=document.querySelector("#bulk-admin");
  const bulkUpdate=groups?.querySelector('[data-config-section="bulk-update"]');
  const cameraImport=groups?.querySelector('[data-config-section="camera-import"]');
  const aiRuntime=groups?.querySelector('[data-config-section="ai-runtime"]');

  const nav=document.createElement("nav");nav.id="camera-workspace-nav";nav.className="camera-workspace-nav";nav.setAttribute("aria-label","Camera settings workspaces");
  for(const [id,label] of [["cameras","Cameras"],["add","Add Cameras"],["groups","Groups & Policies"]]){
    const button=document.createElement("button");button.type="button";button.textContent=label;button.dataset.cameraWorkspaceButton=id;
    if(id===activeCameraWorkspace)button.setAttribute("aria-current","page");
    button.addEventListener("click",()=>setCameraWorkspace(id));nav.append(button);
  }

  const cameras=cameraWorkspace("cameras","Cameras");
  const layout=document.createElement("div");layout.id="camera-admin-layout";layout.className="camera-admin-layout";
  const inventory=document.createElement("section");inventory.className="camera-inventory-pane";inventory.setAttribute("aria-labelledby","camera-inventory-heading");
  const inventoryHeading=document.createElement("h3");inventoryHeading.id="camera-inventory-heading";inventoryHeading.textContent="Camera inventory";inventory.append(inventoryHeading);
  const filters=document.createElement("div");filters.id="camera-inventory-filters";filters.className="camera-inventory-filters";
  const selectFilter=(id,labelText)=>{const label=document.createElement("label"),select=document.createElement("select");label.htmlFor=id;label.textContent=labelText;select.id=id;label.append(select);filters.append(label);return select;};
  const archive=selectFilter("camera-filter-archive","Camera state");
  archive.append(new Option("Active","active"),new Option("Archived","archived"),new Option("All cameras","all"));archive.value=cameraInventoryFilters.archive;
  selectFilter("camera-filter-group","Group");
  selectFilter("camera-filter-connection","Connection");
  selectFilter("camera-filter-health","Recording health");
  inventory.append(filters);
  const groupNote=document.createElement("p");groupNote.className="camera-inventory-note";groupNote.textContent="Group shows the camera’s policy group. Browsing groups can overlap.";inventory.append(groupNote);
  if(searchLabel&&search)inventory.append(searchLabel,search);
  const table=document.createElement("div");table.id="camera-inventory-table";table.className="camera-inventory-table-scroll";table.setAttribute("role","region");table.setAttribute("aria-label","Camera inventory table");table.tabIndex=0;inventory.append(table);
  if(bulkUpdate)inventory.append(bulkUpdate);

  const details=document.createElement("section");details.className="camera-editor-pane";details.setAttribute("aria-labelledby","selected-camera-heading");
  const back=document.createElement("button");back.id="camera-back-to-inventory";back.type="button";back.textContent="Back to Cameras";back.className="camera-back-button";back.addEventListener("click",async()=>{if(!await guardCameraDrafts())return;mobileCameraEditorOpen=false;layout.dataset.mobileEditor="false";requestAnimationFrame(()=>document.querySelector(`#camera-inventory-table button[data-camera-id="${CSS.escape(selectedCameraId||"")}"]`)?.focus());});details.append(back);
  const detailsHeading=document.createElement("h3");detailsHeading.id="selected-camera-heading";detailsHeading.tabIndex=-1;detailsHeading.textContent="Selected camera";details.append(detailsHeading);
  if(editor)details.append(editor);
  const history=document.createElement("section");history.id="camera-health-history";history.className="camera-health-history hidden";history.setAttribute("aria-label","Camera health and recorded footage");
  if(segmentsHeading)history.append(segmentsHeading);
  if(segments)history.append(segments);
  if(player)history.append(player);
  if(message)details.append(message);
  details.append(history);
  layout.append(inventory,details);
  cameras.append(layout);

  const add=cameraWorkspace("add","Add Cameras");
  const addHeading=document.createElement("h3");addHeading.textContent="Add Cameras";add.append(addHeading);
  if(discovery)add.append(discovery);
  if(form)add.append(form);
  if(cameraImport)add.append(cameraImport);

  const groupsWorkspace=cameraWorkspace("groups","Groups & Policies");
  if(groups){const groupHeading=groups.querySelector("#bulk-heading");if(groupHeading)groupHeading.textContent="Groups & Policies";groupsWorkspace.append(groups);}

  content.replaceChildren();
  if(heading)content.append(heading);
  content.append(nav,cameras,add,groupsWorkspace);
  ensureCameraDirtyDialog();
  if(aiRuntime)document.querySelector("#app-settings-content").append(aiRuntime);

  const refreshFilters=()=>{
    const values={
      query:search?.value||"",archive:archive.value,
      group:document.querySelector("#camera-filter-group").value,
      connection:document.querySelector("#camera-filter-connection").value,
      health:document.querySelector("#camera-filter-health").value,
      sortKey:cameraInventoryFilters.sortKey,sortDirection:cameraInventoryFilters.sortDirection,
    };
    Object.assign(cameraInventoryFilters,values);
    renderCameras(latestCameras);
  };
  for(const select of filters.querySelectorAll("select"))select.addEventListener("change",refreshFilters);
}
function setupSettingsCategories() {
  const root=document.querySelector("#app-settings-content");
  if(root.querySelector(".settings-category-nav"))return;
  const categories=[
    ["general","General",["#status-heading","#status-list",".badge","#display-settings-admin"]],
    ["appearance","Appearance",["#branding-admin"]],
    ["users","Users & access",["#identity-settings","#session-admin","#logout"]],
    ["recording","Recording & storage",["#settings-recording-link"]],
    ["notifications","Notifications",["#webhooks-admin","#heartbeat-admin"]],
    ["integrations","Integrations",["#settings-integrations-link"]],
    ["ai-runtime","AI runtime",["#ai-runtime"]],
    ["diagnostics","Diagnostics",["#diagnostics-admin"]],
  ];
  const nav=document.createElement("nav");nav.className="settings-category-nav";nav.setAttribute("aria-label","Settings categories");
  const panels=new Map();
  for(const [id,label,selectors] of categories){
    const button=document.createElement("button");button.type="button";button.textContent=label;button.dataset.settingsCategory=id;
    const panel=document.createElement("div");panel.className="settings-panel";panel.dataset.settingsPanel=id;panel.dataset.configSection=`settings-${id}`;panel.setAttribute("aria-label",label);
    panels.set(id,panel);nav.append(button);root.append(panel);
    for(const selector of selectors){const element=document.querySelector(selector);if(element)panel.append(element);}
    button.addEventListener("click",()=>setSettingsCategory(id,true));
  }
  root.prepend(nav);
  root.querySelectorAll("[data-open-camera-admin]").forEach(button=>button.addEventListener("click",async()=>{
    if(button.dataset.openCameraAdmin==="ai"){
      if(!await setScreen("app-settings"))return;setSettingsCategory("ai-runtime",true);
      requestAnimationFrame(()=>document.querySelector("#ai-runtime")?.scrollIntoView({behavior:"smooth",block:"start"}));
      return;
    }
    if(!await setScreen("camera-settings"))return;
    const changed=await setCameraWorkspace("cameras");if(changed)requestAnimationFrame(()=>{document.querySelector('[data-config-section="bulk-update"]')?.scrollIntoView({behavior:"smooth",block:"start"});});
  }));
  window.addEventListener("hashchange",async()=>{const match=/^#settings\/([a-z-]+)$/.exec(location.hash);if(match&&panels.has(match[1]))setSettingsCategory(match[1],false);});
  const match=/^#settings\/([a-z-]+)$/.exec(location.hash);
  setSettingsCategory(match&&panels.has(match[1])?match[1]:"general",false);
  if(match&&panels.has(match[1]))void setScreen("app-settings");
}
function setSettingsCategory(id,updateHash=false){
  const current=document.querySelector("[data-settings-panel]:not(.hidden)");
  if(current&&current.dataset.settingsPanel!==id&&!guardDraftsIn(current)){
    if(!updateHash&&/^#settings\//.test(location.hash))history.replaceState(null,"",`#settings/${current.dataset.settingsPanel}`);
    return false;
  }
  document.querySelectorAll("[data-settings-panel]").forEach(panel=>panel.classList.toggle("hidden",panel.dataset.settingsPanel!==id));
  document.querySelectorAll("[data-settings-category]").forEach(button=>button.setAttribute("aria-current",String(button.dataset.settingsCategory===id)));
  if(updateHash)history.replaceState(null,"",`#settings/${id}`);
  return true;
}
function setupConsoleShell() {
  if (consoleShellReady) return;
  const live = document.querySelector("#live-page-content"), footage = document.querySelector("#footage-page-content");
  const cameraSettings = document.querySelector("#camera-settings-content"), appSettings = document.querySelector("#app-settings-content");
  for (const selector of ["#live-heading"]) {
    const heading = document.querySelector(selector);
    if (heading) live.append(heading.closest("section"));
  }
  const incidentsHeading=document.querySelector("#incidents-heading");
  if(incidentsHeading)document.querySelector("#ops-incident-content").append(incidentsHeading.closest("section"));
  for (const selector of ["#playback-heading"]) {
    const heading = document.querySelector(selector);
    if (heading) footage.append(heading.closest("section"));
  }
  for (const selector of ["#camera-heading", "#discovery-admin", "#bulk-admin", "#camera-form", "#camera-message", "#camera-inventory-search", "#camera-list", "#segment-list", "#playback"]) {
    if (selector === "#camera-heading") {
      const heading = document.querySelector(selector);
      if (heading) cameraSettings.append(heading);
    } else moveInto(selector, "#camera-settings-content");
  }
  const recordedHeading = [...document.querySelectorAll("h3")].find(item => item.textContent.trim() === "Recorded footage");
  if (recordedHeading) cameraSettings.append(recordedHeading);
  setupCameraAdminWorkspaces();
  moveInto("#status > .badge", "#app-settings-content");
  for (const selector of ["#status-heading", "#status-list", "#session-admin", "#diagnostics-admin", "#webhooks-admin", "#heartbeat-admin", "#display-settings-admin", "#branding-admin", "#identity-settings", "#settings-recording-link", "#settings-integrations-link", "#logout"]) moveInto(selector, "#app-settings-content");
  setupSettingsCategories();
  for(const selector of ["#discovery-admin","#camera-form","#group-form","#display-settings-admin","#branding-admin","#diagnostics-admin"]){const element=document.querySelector(selector);if(element)element.dataset.configSection=selector.slice(1);}
  for(const [selector,sections] of Object.entries({"#brand-save":"branding-admin","#brand-reset":"branding-admin","#display-settings-save":"display-settings-admin","#diagnostics-toggle":"diagnostics-admin","#webhook-create":"webhook-create","#heartbeat-save":"heartbeat-config","#save-group-schedule":"group-schedule","#clear-group-schedule":"group-schedule","#save-group-retention":"group-retention","#clear-group-retention":"group-retention","#save-storage-volume":"group-storage","#save-group-ai":"group-ai","#save-ai-runtime":"ai-runtime","#apply-bulk":"bulk-update","#apply-import":"camera-import"})){const button=document.querySelector(selector);if(button)button.dataset.configSaves=sections;}
  document.addEventListener("input",markConfigDraft,true);
  document.addEventListener("change",markConfigDraft,true);
  window.addEventListener("beforeunload",event=>{if(document.querySelector('[data-config-dirty="true"]')){event.preventDefault();event.returnValue="";}});
  document.querySelectorAll("[data-screen]").forEach(button => button.addEventListener("click", () => void setScreen(button.dataset.screen)));
  const statusToggle=document.querySelector("#ops-status-toggle"),statusDrawer=document.querySelector("#ops-status-drawer");
  statusToggle.addEventListener("click",()=>{const opening=statusDrawer.classList.contains("hidden");statusDrawer.classList.toggle("hidden",!opening);statusToggle.setAttribute("aria-expanded",String(opening));if(opening){refreshExports();if(currentRole==="administrator")refreshOperationalStorage();document.querySelector("#ops-status-close").focus();}});
  document.querySelector("#ops-status-close").addEventListener("click",()=>{statusDrawer.classList.add("hidden");statusToggle.setAttribute("aria-expanded","false");statusToggle.focus();});
  document.querySelector("#ops-storage-settings").addEventListener("click",async()=>{if(await setScreen("app-settings"))setSettingsCategory("recording",true);});
  document.querySelector("#sidebar-toggle").addEventListener("click", () => {
    const layout = document.querySelector("#console-layout"), button = document.querySelector("#sidebar-toggle");
    const collapsed = layout.classList.toggle("sidebar-collapsed");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "Expand camera sidebar" : "Collapse camera sidebar");
    saveLiveWorkspace();
  });
  document.querySelector("#sidebar-search").addEventListener("input", filterSidebar);
  const inventorySearch=document.querySelector("#camera-inventory-search");let lastInventoryQuery=inventorySearch.value;
  inventorySearch.addEventListener("input",()=>{lastInventoryQuery=inventorySearch.value;cameraInventoryFilters.query=lastInventoryQuery;renderCameras(latestCameras);});
  document.querySelector("#live-quality").addEventListener("change",()=>startLiveViews(activeLiveCameraIds,latestCameras,focusedLiveCameraId,activeLiveGridSize,activeLiveTileCameraIds,activeLiveLayoutMode));
  const theme = document.querySelector("#theme-choice"), savedTheme = localStorage.getItem("nvr-theme") || "system";
  theme.value = savedTheme; document.documentElement.dataset.theme = savedTheme === "system" ? "" : savedTheme;
  theme.addEventListener("change", () => { localStorage.setItem("nvr-theme", theme.value); document.documentElement.dataset.theme = theme.value === "system" ? "" : theme.value; applyBranding(currentBranding);updateBrandingDraftPreview(); });
  for(const selector of ["#brand-app-name","#brand-primary-color","#brand-accent-color"])document.querySelector(selector).addEventListener("input",updateBrandingDraftPreview);
  document.querySelectorAll("[data-brand-asset]").forEach(input=>input.addEventListener("change",()=>{delete document.querySelector(`[data-remove-brand-asset='${input.dataset.brandAsset}']`).dataset.removed;updateBrandingDraftPreview();}));
  document.querySelectorAll("[data-remove-brand-asset]").forEach(button=>button.addEventListener("click",()=>{button.dataset.removed="true";document.querySelector(`[data-brand-asset='${button.dataset.removeBrandAsset}']`).value="";updateBrandingDraftPreview();}));
  document.querySelector("#brand-save").addEventListener("click",()=>saveBrandingSettings());
  document.querySelector("#brand-reset").addEventListener("click",()=>saveBrandingSettings(true));
  for(const selector of ["#display-time-zone","#display-date-format","#display-clock-format"])document.querySelector(selector).addEventListener("input",previewDisplaySettings);
  document.querySelector("#display-settings-save").addEventListener("click",saveDisplaySettings);
  for(const field of ["start","end"])document.querySelector(`#playback-${field}`).addEventListener("change",()=>{updateFoldChoices(field);markPlaybackResultsStale();savePlaybackWorkspace();});
  for(const field of ["start","end"])document.querySelector(`#playback-${field}-fold`).addEventListener("change",()=>{markPlaybackResultsStale();savePlaybackWorkspace();});
  for(const selector of ["#playback-start","#playback-end"])document.querySelector(selector).addEventListener("input",()=>{markPlaybackResultsStale();savePlaybackWorkspace();});
  document.querySelector("#playback-camera-picker").addEventListener("change",event=>{
    const selected=[...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value);
    if(selected.length>16){
      requestCameraSubset(selected,selected.slice(0,16),chosen=>{
        const keep=new Set(chosen);for(const input of document.querySelectorAll("[data-playback-camera]")){input.checked=keep.has(input.value);playbackManualChoices.set(input.value,input.checked);}
        syncPlaybackGroupChecks();markPlaybackResultsStale();savePlaybackWorkspace();renderSidebar();if(playbackTimeline)loadSynchronizedPlayback({preserve:true});
      });return;
    }
    if(event.target.matches("[data-playback-camera]")){
      playbackManualChoices.set(event.target.value,event.target.checked);
      syncPlaybackGroupChecks();markPlaybackResultsStale();
    } else if(event.target.matches("[data-playback-group]")){
      const group=sidebarGroups.find(item=>item.id===event.target.value),members=new Set(group?.camera_ids||[]);
      for(const cameraId of members)playbackManualChoices.delete(cameraId);
      syncPlaybackCameraChecks();markPlaybackResultsStale();
    }
    savePlaybackWorkspace();renderSidebar();
    if(playbackTimeline)loadSynchronizedPlayback({preserve:true});
  });
  document.querySelector("#playback-grid-size").addEventListener("change",()=>{markPlaybackResultsStale();updatePlaybackGridLayout();savePlaybackWorkspace();});
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
function effectiveTheme(){
  const chosen=document.documentElement.dataset.theme;
  return chosen==="light"||chosen==="dark"?chosen:(window.matchMedia?.("(prefers-color-scheme: light)").matches?"light":"dark");
}
function contrastInk(hex){
  const rgb=hex.slice(1).match(/.{2}/g).map(value=>parseInt(value,16)/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);
  const luminance=.2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];
  return (1.05/(luminance+.05))>((luminance+.05)/.05)?"#fff":"#000";
}
function applyBranding(branding){
  currentBranding=branding||currentBranding;
  const appName=currentBranding.app_name||"Workplace NVR";
  document.title=appName;
  document.querySelector("#brand-header-name").textContent=appName;
  document.querySelector("#brand-signin-name").textContent=appName;
  document.documentElement.style.setProperty("--brand-primary",currentBranding.primary_color||"#17212b");
  document.documentElement.style.setProperty("--accent",currentBranding.accent_color||"#69a1ff");
  document.documentElement.style.setProperty("--accent-ink",contrastInk(currentBranding.accent_color||"#69a1ff"));
  const variant=effectiveTheme()==="dark"?"dark":"light",assets=currentBranding.assets||{};
  const logo=assets[variant]?.url||assets.primary?.url||"";
  for(const image of [document.querySelector("#brand-header-logo"),document.querySelector("#brand-signin-logo")]){
    if(logo){image.src=logo;image.classList.remove("hidden");image.onerror=()=>image.classList.add("hidden");}
    else {image.removeAttribute("src");image.classList.add("hidden");}
  }
  const favicon=document.querySelector("#brand-favicon");
  if(assets.icon?.url){favicon.href=assets.icon.url;favicon.type=assets.icon.media_type;}
  else{favicon.removeAttribute("href");favicon.removeAttribute("type");}
}
async function refreshPublicBranding(){
  try{applyBranding(await api("/api/public/branding"));}
  catch{applyBranding(currentBranding);}
}
function sniffBrandingType(bytes){
  if(bytes.length>8&&bytes[0]===137&&bytes[1]===80&&bytes[2]===78&&bytes[3]===71)return "image/png";
  if(bytes.length>3&&bytes[0]===255&&bytes[1]===216&&bytes[2]===255)return "image/jpeg";
  if(bytes.length>12&&String.fromCharCode(...bytes.slice(0,4))==="RIFF"&&String.fromCharCode(...bytes.slice(8,12))==="WEBP")return "image/webp";
  return "";
}
async function fileAsBrandingAsset(file){
  if(file.size>2*1024*1024)throw new Error(`${file.name} is larger than 2 MiB.`);
  const bytes=new Uint8Array(await file.arrayBuffer()),media_type=sniffBrandingType(bytes);
  if(!media_type||file.type&&file.type!==media_type)throw new Error(`${file.name} is not a valid PNG, WebP, or JPEG file.`);
  let binary="";for(let offset=0;offset<bytes.length;offset+=0x8000)binary+=String.fromCharCode(...bytes.subarray(offset,offset+0x8000));
  return {media_type,data_base64:btoa(binary)};
}
function updateBrandingDraftPreview(){
  const preview=document.querySelector("#branding-preview"),image=preview.querySelector("img"),name=document.querySelector("#brand-app-name").value.trim()||"Application preview";
  const primary=document.querySelector("#brand-primary-color").value,accent=document.querySelector("#brand-accent-color").value;
  preview.style.setProperty("--preview-primary",primary);preview.style.setProperty("--preview-accent",accent);preview.style.borderTop=`3px solid ${primary}`;
  preview.style.backgroundColor=effectiveTheme()==="dark"?"#121922":"#fff";preview.style.color=effectiveTheme()==="dark"?"#eef3f8":"#15202b";
  preview.querySelector("strong").textContent=name;
  const variant=effectiveTheme(),saved=currentBranding.assets?.[variant]||currentBranding.assets?.primary;
  const file=document.querySelector("[data-brand-asset='primary']").files[0]||document.querySelector(`[data-brand-asset='${variant}']`).files[0];
  const removal=[...document.querySelectorAll("[data-remove-brand-asset]")].some(button=>button.dataset.removed==="true"&&["primary",variant].includes(button.dataset.removeBrandAsset));
  let source=file?URL.createObjectURL(file):removal?"":saved?.url||"";
  if(brandingPreviewUrl){URL.revokeObjectURL(brandingPreviewUrl);brandingPreviewUrl=null;}
  if(file)brandingPreviewUrl=source;
  if(source){image.src=source;image.classList.remove("hidden");image.alt=`${name} logo preview`;image.onerror=()=>image.classList.add("hidden");}
  else{image.removeAttribute("src");image.classList.add("hidden");}
}
async function loadBrandingSettings(){
  try{
    currentBranding=await api("/api/settings/branding");
    document.querySelector("#brand-app-name").value=currentBranding.app_name;
    document.querySelector("#brand-primary-color").value=currentBranding.primary_color;
    document.querySelector("#brand-accent-color").value=currentBranding.accent_color;
    document.querySelectorAll("[data-brand-asset]").forEach(input=>input.value="");
    document.querySelectorAll("[data-remove-brand-asset]").forEach(button=>delete button.dataset.removed);
    document.querySelector("#branding-message").textContent="Saved branding loaded. Draft changes stay in this editor until saved.";
    updateBrandingDraftPreview();
  }catch(error){document.querySelector("#branding-message").textContent="Branding settings could not be loaded: "+error.message;}
}
async function saveBrandingSettings(resetDefaults=false){
  const button=document.querySelector("#brand-save"),message=document.querySelector("#branding-message");
  button.disabled=true;message.textContent=resetDefaults?"Resetting branding…":"Saving branding…";
  try{
    const payload={app_name:document.querySelector("#brand-app-name").value,primary_color:document.querySelector("#brand-primary-color").value,accent_color:document.querySelector("#brand-accent-color").value,assets:{},reset_defaults:resetDefaults};
    if(!resetDefaults){
      for(const input of document.querySelectorAll("[data-brand-asset]")){
        const key=input.dataset.brandAsset,file=input.files[0],remove=document.querySelector(`[data-remove-brand-asset='${key}']`).dataset.removed==="true";
        if(remove)payload.assets[key]=null;else if(file)payload.assets[key]=await fileAsBrandingAsset(file);
      }
    }
    const result=await api("/api/settings/branding",{method:"PUT",body:JSON.stringify(payload)});
    applyBranding(result);currentBranding=result;
    document.querySelectorAll("[data-brand-asset]").forEach(input=>input.value="");
    document.querySelectorAll("[data-remove-brand-asset]").forEach(item=>delete item.dataset.removed);
    document.querySelector("#brand-app-name").value=result.app_name;document.querySelector("#brand-primary-color").value=result.primary_color;document.querySelector("#brand-accent-color").value=result.accent_color;
    message.textContent="Branding saved and published.";updateBrandingDraftPreview();
  }catch(error){message.textContent="Branding was not saved: "+error.message;}
  finally{button.disabled=false;}
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
async function setScreen(screen) {
  if (!document.querySelector(`[data-page="${screen}"]`)) return false;
  const changing=screen!==activeScreen;
  if(changing&&!await guardScreenDrafts(activeScreen))return false;
  if(changing&&activeScreen==="camera-settings"&&screen!=="camera-settings")cameraPreviewWorkspace.leave();
  if (activeScreen === "live" && screen !== "live") {
    if(activeLiveCameraIds.length)cachedLiveArrangement={camera_ids:[...activeLiveCameraIds],tile_camera_ids:[...activeLiveTileCameraIds],grid_size:activeLiveGridSize,layout_mode:activeLiveLayoutMode};
    stopLiveViews();
  }
  if (activeScreen === "footage" && screen !== "footage") savePlaybackWorkspace();
  if (screen !== "footage") stopSynchronizedPlayback();
  activeScreen = screen;
  if(changing&&screen==="camera-settings"&&!document.querySelector('#camera-settings-content [data-config-dirty="true"]')){
    renderCameras(latestCameras);renderGroupManagement(sidebarGroups);
  }
  document.querySelectorAll("[data-page]").forEach(page => page.classList.toggle("hidden", page.dataset.page !== screen));
  document.querySelectorAll("#console-nav [data-screen]").forEach(button => {
    if (button.classList.contains("hidden")) return;
    if (button.dataset.screen === screen) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if(screen==="live"&&cachedLiveArrangement&&!activeLiveCameraIds.length){
    const prior=cachedLiveArrangement;cachedLiveArrangement=null;
    for(const input of document.querySelectorAll("[data-live-camera]"))input.checked=prior.camera_ids.includes(input.value);
    startLiveViews(prior.camera_ids,latestCameras,null,prior.grid_size,prior.tile_camera_ids,prior.layout_mode||"auto");
    clearLiveViewDirty();
  }
  if(sidebarCameras.length)renderSidebar();
  return true;
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
function currentWorkspaceCameraIds() {
  return activeScreen === "footage"
    ? [...document.querySelectorAll("[data-playback-camera]:checked")].map(input => input.value)
    : [...activeLiveCameraIds];
}
function applySidebarCameraSelection(nextIds) {
  if (activeScreen === "footage") {
    const chosen = new Set(nextIds);
    for (const input of document.querySelectorAll("[data-playback-camera]")) {
      input.checked = chosen.has(input.value);
      playbackManualChoices.set(input.value, input.checked);
    }
    syncPlaybackGroupChecks();
    markPlaybackResultsStale();
    savePlaybackWorkspace();
    renderSidebar();
    if(playbackTimeline)loadSynchronizedPlayback({preserve:true});
    return;
  }
  const mode = document.querySelector("#live-grid-size").value === "auto" ? "auto" : "manual";
  const ordered = reconcileCameraOrder(activeLiveCameraIds, nextIds);
  if (ordered.length > 16) {
    requestCameraSubset(ordered, activeLiveCameraIds, chosen => applySidebarCameraSelection(chosen));
    return;
  }
  if (mode === "manual" && ordered.length > Number(document.querySelector("#live-grid-size").value)) {
    document.querySelector("#live-grid-size").value = "auto";
    startLiveViews(ordered, latestCameras, null, null, null, "auto");
    document.querySelector("#live-message").textContent = "Auto layout restored to keep every selected camera visible.";
  } else {
    startLiveViews(ordered, latestCameras, focusedLiveCameraId, null, activeLiveTileCameraIds, mode);
  }
  renderSidebar();
}
function renderSidebar() {
  const cameraList = document.querySelector("#sidebar-cameras"), groupList = document.querySelector("#sidebar-groups"), viewList = document.querySelector("#sidebar-views");
  cameraList.replaceChildren(); groupList.replaceChildren(); viewList.replaceChildren();
  for (const camera of [...sidebarCameras].filter(camera=>!camera.archived_at).sort((a, b) => a.name.localeCompare(b.name))) {
    const address=camera.camera_ip||"";
    const selected = ["camera-settings","app-settings"].includes(activeScreen)?selectedCameraId===camera.id:currentWorkspaceCameraIds().includes(camera.id);
    const row=sidebarItem(cameraList, camera.name, `${camera.name} ${address}`, async () => {
      if(activeScreen==="camera-settings"||activeScreen==="app-settings"){
        if(selectedCameraId===camera.id){if(activeScreen==="app-settings"&&!await setScreen("camera-settings"))return;if(activeScreen==="camera-settings")await setCameraWorkspace("cameras");mobileCameraOpen(camera.id);return;}
        if(selectedCameraId!==camera.id&&!await guardCameraDrafts())return;
        if(activeScreen==="app-settings"&&!await setScreen("camera-settings"))return;
        if(!await setCameraWorkspace("cameras"))return;
        selectedCameraId=camera.id;cameraEditorState.selectCamera(camera.id);clearCameraHistory();mobileCameraEditorOpen=true;renderCameras(latestCameras);renderSidebar();return;
      }
      const current = currentWorkspaceCameraIds();
      applySidebarCameraSelection(current.includes(camera.id) ? current.filter(id => id !== camera.id) : [...current, camera.id]);
    }, camera.recording_health?.state || camera.recording_status || "unknown");
    const button=row.querySelector("button");
    button.setAttribute("aria-pressed",String(selected));
    button.setAttribute("aria-label",["camera-settings","app-settings"].includes(activeScreen)?`Open settings for ${camera.name}`:`${selected?"Remove":"Add"} ${camera.name} ${activeScreen==="footage"?"from":"in"} ${activeScreen==="footage"?"Playback":"Live"}`);
    if(selected){const marker=document.createElement("span");marker.className="camera-selected-marker";marker.textContent="✓";marker.setAttribute("aria-hidden","true");button.append(marker);}
    row.draggable=true;
    row.addEventListener("dragstart",event=>event.dataTransfer.setData("text/plain",camera.id));
  }
  for (const group of sidebarGroups) sidebarItem(groupList, group.name, group.name, () => {
    const available=new Set(latestCameras.filter(camera=>!camera.archived_at).map(camera=>camera.id));
    const members=[...new Set(group.camera_ids||[])].filter(id=>available.has(id));
    if(members.length>16){requestCameraSubset(members,currentWorkspaceCameraIds(),applySidebarCameraSelection);return;}
    applySidebarCameraSelection(members);
    if (!members.length) document.querySelector(activeScreen==="footage"?"#playback-status":"#live-message").textContent = "This group has no available cameras.";
  });
  for (const view of sidebarViews) sidebarItem(viewList, `${view.name} · ${view.scope}`, `${view.name} ${view.scope}`, () => applySavedView(view));
  filterSidebar();
}
function updatePlaybackGridLayout(cameraCount=playbackViews.length) {
  const grid=document.querySelector("#playback-grid");
  if(!grid)return;
  const layout=document.querySelector("#playback-grid-size").value;
  const columns=layout==="auto"
    ? autoLayoutForCount(Math.min(16,Math.max(0,cameraCount)),grid.clientWidth||window.innerWidth).columns
    : Math.ceil(Math.sqrt(Number(layout)||4));
  grid.style.setProperty("--playback-grid-columns",String(columns));
}
async function showLogin() {
  await refreshPublicBranding();
  const state = await api("/api/setup/status");
  show(state.setup_required ? "setup" : "login");
  if (!state.setup_required) {
    try { document.querySelector("#entra-login").classList.toggle("hidden", !(await api("/api/auth/oidc/status")).enabled); }
    catch { document.querySelector("#entra-login").classList.add("hidden"); }
  }
}
function markConfigDraft(event){
  if(!(event.target instanceof HTMLElement)||event.target.type==="search"||event.target.id==="schedule-group")return;
  markConfigDraftElement(event.target);
}
function markConfigDraftElement(target){
  const scope=target?.closest?.("[data-config-section]");
  if(!scope)return;
  scope.dataset.configDirty="true";
  if(!scope.querySelector(":scope > .unsaved-draft-notice")){
    const notice=document.createElement("p");notice.className="unsaved-draft-notice";notice.dataset.unsavedDraftNotice="";notice.setAttribute("role","status");notice.textContent="Unsaved changes";
    const label=scope.matches("details,fieldset")?scope.querySelector(":scope > summary, :scope > legend"):null;
    if(label)label.after(notice);else scope.prepend(notice);
  }
  const tab=window.NvrCameraEditor.CAMERA_EDITOR_TABS.find(item=>item.sections.includes(scope.dataset.configSection));if(tab)setCameraTabStatus(tab.id,"Unsaved changes","dirty");
  refreshCameraTabStatuses();
}
function clearConfigDraft(scope){
  if(!scope)return;
  delete scope.dataset.configDirty;
  scope.querySelector(":scope > .unsaved-draft-notice")?.remove();
  refreshCameraTabStatuses();
}
function guardDraftsIn(root){
  const candidates=[...(root?.matches?.('[data-config-dirty="true"]')?[root]:[]),...(root?.querySelectorAll?.('[data-config-dirty="true"]')||[])];
  const dirty=candidates.filter(scope=>!scope.closest("[data-settings-panel].hidden"));
  if(!dirty.length)return true;
  if(!window.confirm("Discard unsaved configuration changes?"))return false;
  dirty.forEach(clearConfigDraft);return true;
}
function ensureCameraDirtyDialog(){
  if(cameraDirtyDecisionDialog)return cameraDirtyDecisionDialog;
  const dialog=document.createElement("dialog");dialog.id="camera-dirty-decision";dialog.setAttribute("aria-labelledby","camera-dirty-decision-title");
  const heading=document.createElement("h2");heading.id="camera-dirty-decision-title";heading.textContent="Unsaved camera settings";
  const description=document.createElement("p");description.textContent="Save or discard your changes before continuing.";
  const status=document.createElement("p");status.dataset.dialogStatus="";status.setAttribute("role","status");status.setAttribute("aria-live","polite");
  const actions=document.createElement("div");actions.className="toolbar-row";
  for(const [value,label,className] of [["cancel","Cancel",""],["discard","Discard",""],["save","Save","primary"]]){
    const button=document.createElement("button");button.type="button";button.dataset.decision=value;button.textContent=label;if(className)button.className=className;
    button.addEventListener("click",()=>{if(cameraDirtyDecisionResolve){const resolve=cameraDirtyDecisionResolve;cameraDirtyDecisionResolve=null;dialog.close();resolve(value);}});actions.append(button);
  }
  dialog.append(heading,description,status,actions);
  dialog.addEventListener("cancel",event=>{event.preventDefault();if(cameraDirtyDecisionResolve){const resolve=cameraDirtyDecisionResolve;cameraDirtyDecisionResolve=null;dialog.close();resolve("cancel");}});
  document.body.append(dialog);cameraDirtyDecisionDialog=dialog;return dialog;
}
function askCameraDirtyDecision(){
  if(cameraDirtyDecisionPromise)return cameraDirtyDecisionPromise;
  const dialog=ensureCameraDirtyDialog();
  const status=dialog.querySelector("[data-dialog-status]");status.textContent="";
  cameraDirtyDecisionPromise=new Promise(resolve=>{cameraDirtyDecisionResolve=value=>{cameraDirtyDecisionResolve=null;cameraDirtyDecisionPromise=null;resolve(value);};dialog.showModal();dialog.querySelector('[data-decision="save"]').focus();});
  return cameraDirtyDecisionPromise;
}
function cameraDirtyScopes(root=document.querySelector("#camera-list")){
  const nodes=[...(root?.matches?.('[data-config-dirty="true"]')?[root]:[]),...(root?.querySelectorAll?.('[data-config-dirty="true"]')||[])];
  return nodes.filter(scope=>scope.dataset.configSection&&scope.closest("#camera-list"));
}
function setCameraTabStatus(tabId,text,state="saved"){
  const status=document.querySelector(`[data-camera-tab-status="${tabId}"]`);if(!status)return;
  status.textContent=text;status.dataset.state=state;
}
function refreshCameraTabStatuses(){
  for(const tab of window.NvrCameraEditor.CAMERA_EDITOR_TABS){
    const dirty=cameraEditorState.dirtySections(tab.id,cameraDirtyScopes().map(scope=>scope.dataset.configSection));
    const status=document.querySelector(`[data-camera-tab-status="${tab.id}"]`);
    const save=document.querySelector(`[data-camera-tab-save="${tab.id}"]`);
    const revert=document.querySelector(`[data-camera-tab-revert="${tab.id}"]`);
    if(save)save.disabled=!dirty.length||cameraEditorSaveInProgress||currentRole!=="administrator";
    if(revert)revert.disabled=!dirty.length||cameraEditorSaveInProgress||currentRole!=="administrator";
    if(!dirty.length)setCameraTabStatus(tab.id,"Saved","saved");
    else if(status?.dataset.state==="saving"&&cameraEditorSaveInProgress)continue;
    else if(status?.dataset.state!=="error")setCameraTabStatus(tab.id,`${dirty.length} unsaved section${dirty.length===1?"":"s"}`,"dirty");
  }
}
function cameraScopeLabel(scope){
  return scope?.querySelector(":scope > legend, :scope > summary")?.textContent?.trim()||scope?.dataset.configSection||"Camera settings";
}
function lockCameraScopes(scopes){
  const controls=new Map();
  for(const scope of scopes){
    for(const control of scope.querySelectorAll("input,select,textarea,button")){
      if(!controls.has(control)){controls.set(control,control.disabled);control.disabled=true;}
    }
  }
  return ()=>{for(const [control,wasDisabled] of controls)control.disabled=wasDisabled;};
}
async function saveCameraScopes(scopes,{refreshAfter=false,statusTabId=null}={}){
  const cameraId=selectedCameraId;
  if(!cameraId||cameraEditorSaveInProgress)return false;
  const unique=[];const seen=new Set();
  for(const scope of scopes){
    const group=scope.dataset.cameraSaveGroup||scope.dataset.configSection;
    if(seen.has(group))continue;seen.add(group);unique.push({scope,group});
  }
  if(!unique.length)return true;
  let failure=null,failedGroup=null;
  const savedLabels=[];
  const lockScopes=unique.flatMap(({group,scope})=>{
    const siblings=[...document.querySelectorAll("#camera-list [data-config-section]")].filter(item=>(item.dataset.cameraSaveGroup||item.dataset.configSection)===group);
    return siblings.length?siblings:[scope];
  });
  const unlock=lockCameraScopes(lockScopes);
  const affectedTabIds=new Set(unique.map(({scope})=>window.NvrCameraEditor.CAMERA_EDITOR_TABS.find(tab=>tab.sections.includes(scope.dataset.configSection))?.id).filter(Boolean));
  if(statusTabId)affectedTabIds.add(statusTabId);
  affectedTabIds.forEach(tabId=>setCameraTabStatus(tabId,"Saving…","saving"));
  cameraEditorSaveInProgress=true;refreshCameraTabStatuses();
  try{
    for(const {scope,group} of unique){
      failedGroup=group;
      const save=scope.cameraSaveConfig;
      if(typeof save!=="function")throw new Error(`Saving ${scope.dataset.configSection} is not available.`);
      await save();
      if(selectedCameraId!==cameraId)throw new Error("Camera selection changed while settings were saving. Reload the camera settings before trying again.");
      const groupScopes=[...document.querySelectorAll(`#camera-list [data-config-section]`)].filter(item=>(item.dataset.cameraSaveGroup||item.dataset.configSection)===group);
      savedLabels.push([...groupScopes].map(cameraScopeLabel).join(" and ")||cameraScopeLabel(scope));
      groupScopes.forEach(clearConfigDraft);
    }
    if(refreshAfter)await refresh();
    return true;
  }catch(error){
    failure=error;
    if(savedLabels.length===unique.length){
      failure=null;
      const message=document.querySelector("#camera-message");if(message)message.textContent=`Settings saved, but the camera view could not refresh: ${error.message}`;
      return true;
    }
    const failedScope=unique.find(item=>item.group===failedGroup)?.scope;
    const failedLabel=cameraScopeLabel(failedScope);
    const message=document.querySelector("#camera-message");
    if(message)message.textContent=savedLabels.length
      ? `${savedLabels.join(", ")} saved. ${failedLabel} failed: ${error.message} Remaining changes are still available to save.`
      : `${failedLabel} could not be saved: ${error.message} Your changes are still available.`;
    return false;
  }finally{
    unlock();cameraEditorSaveInProgress=false;refreshCameraTabStatuses();
    if(failure){
      const failedScope=unique.find(item=>item.group===failedGroup)?.scope;
      const failedTab=window.NvrCameraEditor.CAMERA_EDITOR_TABS.find(tab=>tab.sections.includes(failedScope?.dataset.configSection))?.id||statusTabId||cameraEditorState.activeTab;
      setCameraTabStatus(failedTab,`Save failed: ${failure.message}`,"error");
    }
  }
}
async function saveCameraTab(tabId=cameraEditorState.activeTab,{refreshAfter=true}={}){
  const sectionIds=cameraEditorState.sectionsForTab(tabId);
  const dirty=new Set(cameraDirtyScopes().map(scope=>scope.dataset.configSection));
  const scopes=sectionIds.filter(id=>dirty.has(id)).map(id=>document.querySelector(`#camera-list [data-config-section="${id}"]`)).filter(Boolean);
  if(!scopes.length)return true;
  setCameraTabStatus(tabId,"Saving…","saving");
  const saved=await saveCameraScopes(scopes,{refreshAfter,statusTabId:tabId});
  if(saved)setCameraTabStatus(tabId,"Saved","saved");
  return saved;
}
async function saveAllCameraDrafts(){
  const dirtySections=new Set(cameraDirtyScopes().map(scope=>scope.dataset.configSection));
  const scopes=window.NvrCameraEditor.CAMERA_EDITOR_TABS.flatMap(tab=>tab.sections).filter(id=>dirtySections.has(id)).map(id=>document.querySelector(`#camera-list [data-config-section="${id}"]`)).filter(Boolean);
  return saveCameraScopes(scopes,{refreshAfter:true});
}
function activateCameraTab(card,tabId){
  if(!card||!cameraEditorState.selectTab(tabId))return false;
  for(const button of card.querySelectorAll('[role="tab"]')){
    const active=button.dataset.cameraTab===tabId;
    button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;
  }
  for(const panel of card.querySelectorAll("[data-camera-tab-panel]"))panel.classList.toggle("hidden",panel.dataset.cameraTabPanel!==tabId);
  document.querySelector("#camera-health-history")?.classList.toggle("hidden",tabId!=="health-history");
  refreshCameraTabStatuses();
  return true;
}
async function navigateCameraTab(tabId){
  if(tabId===cameraEditorState.activeTab)return true;
  if(!await guardCameraDrafts()){
    document.querySelector(`[role="tab"][data-camera-tab="${CSS.escape(cameraEditorState.activeTab||"")}"]`)?.focus();
    return false;
  }
  const card=document.querySelector(`#camera-list article[data-camera-id="${CSS.escape(selectedCameraId||"")}"]`);
  if(!card||selectedCameraId!==cameraEditorState.selectedCameraId)return false;
  return activateCameraTab(card,tabId);
}
async function revertCameraTab(tabId){
  const dirtyIds=new Set(cameraEditorState.sectionsForTab(tabId));
  const oldDirty=cameraDirtyScopes().filter(scope=>dirtyIds.has(scope.dataset.configSection));
  if(!oldDirty.length||!selectedCameraId)return true;
  if(cameraEditorSaveInProgress)return false;
  const selection=cameraEditorState.captureSelection();
  const unlock=lockCameraScopes(oldDirty);
  cameraEditorSaveInProgress=true;refreshCameraTabStatuses();
  setCameraTabStatus(tabId,"Loading saved settings…","saving");
  try{
    const [inventory,schedule,retention,ai]=await (async()=>{
      if(tabId==="schedule-retention")return Promise.all([api("/api/cameras"),api(`/api/cameras/${encodeURIComponent(selectedCameraId)}/schedule`),api(`/api/cameras/${encodeURIComponent(selectedCameraId)}/retention`)]).then(result=>[...result,null]);
      if(tabId==="ai")return Promise.all([api("/api/cameras"),api(`/api/cameras/${encodeURIComponent(selectedCameraId)}/ai`)]).then(([listed,config])=>[listed,null,null,config]);
      return [await api("/api/cameras"),null,null,null];
    })();
    if(!cameraEditorState.isCurrent(selection))return false;
    const cameras=inventory.cameras||[];
    if(!cameras.some(camera=>camera.id===selection.cameraId))throw new Error("This camera is no longer available.");
    oldDirty.forEach(clearConfigDraft);
    latestCameras=cameras;sidebarCameras=cameras;
    renderCameras(cameras);
    const card=document.querySelector(`#camera-list article[data-camera-id="${CSS.escape(selection.cameraId)}"]`);
    if(!card||!cameraEditorState.isCurrent(selection))return false;
    if(schedule){card.querySelector('[data-config-section="camera-schedule"]')?.cameraApplyConfig?.(schedule);}
    if(retention){card.querySelector('[data-config-section="camera-retention"]')?.cameraApplyConfig?.(retention);}
    if(ai){await card.querySelector('[data-config-section="camera-ai"]')?.cameraLoadConfig?.(ai);}
    setCameraTabStatus(tabId,"Reverted to saved settings","saved");
    refreshCameraTabStatuses();
    return true;
  }catch(error){
    setCameraTabStatus(tabId,`Revert failed: ${error.message}`,"error");
    const message=document.querySelector("#camera-message");if(message)message.textContent=error.message;
    return false;
  }finally{unlock();cameraEditorSaveInProgress=false;refreshCameraTabStatuses();}
}
async function guardCameraDrafts(root=document.querySelector("#camera-list")){
  if(cameraEditorSaveInProgress)return false;
  const dirty=cameraDirtyScopes(root);if(!dirty.length)return true;
  const decision=await askCameraDirtyDecision();
  return window.NvrCameraEditor.resolveDirtyNavigation(decision,{
    save:()=>saveAllCameraDrafts(),
    discard:async()=>{dirty.forEach(clearConfigDraft);renderCameras(latestCameras);},
  });
}
async function guardScreenDrafts(screen){
  if(screen==="app-settings")return guardDraftsIn(document.querySelector("#app-settings-content"));
  if(screen==="camera-settings")return guardCameraDrafts();
  return true;
}
function clearSavedConfigDraft(path,options,button){
  const method=(options.method||"GET").toUpperCase();
  if(!["POST","PUT","PATCH","DELETE"].includes(method))return;
  if(!button)return;
  const names=(button.dataset.configSaves||"").split(/\s+/).filter(Boolean);
  if(names.length){
    const camera=button.closest("[data-camera-id]");
    for(const name of names){
      const scope=button.closest(`[data-config-section="${name}"]`)||camera?.querySelector(`[data-config-section="${name}"]`)||document.querySelector(`[data-config-section="${name}"]`);
      clearConfigDraft(scope);
    }
    return;
  }
}
async function api(path, options = {}) {
  const actionButton=document.activeElement?.closest?.("button");
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers }, credentials: "same-origin" });
  if (response.status === 204) {clearSavedConfigDraft(path,options,actionButton);return null;}
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  clearSavedConfigDraft(path,options,actionButton);
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
    await refreshPublicBranding();
    const who = await api("/api/auth/me");
    currentRole = who.role;
    currentUsername = who.username;
    const admin = currentRole === "administrator";
    document.querySelector("#live-layout-scope option[value=shared]").classList.toggle("hidden", !admin);
    setupConsoleShell();
    document.querySelectorAll('#console-nav [data-screen="camera-settings"], #console-nav [data-screen="app-settings"]').forEach(button => button.classList.toggle("hidden", !admin));
    if (!admin && ["camera-settings", "app-settings"].includes(activeScreen)) await setScreen("live");
    document.querySelector("#status-heading").textContent = admin ? `System status · ${who.username}` : `Cameras · ${who.username}`;
    document.querySelector("#status-list").classList.toggle("hidden", !admin);
    document.querySelector("#ops-storage-section").classList.toggle("hidden", !admin);
    if(!admin)latestStorageStatus=null;
    document.querySelector("#display-settings-admin").classList.toggle("hidden", !admin);
    document.querySelector("#branding-admin").classList.toggle("hidden", !admin);
    for(const selector of ["#identity-settings","#settings-recording-link","#settings-integrations-link"])document.querySelector(selector).classList.toggle("hidden",!admin);
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
      latestSystemStatus=data;
      const rows = [["Version", data.version], ["Database", data.database], ["Administrators", data.configured_administrators], ["Storage available", data.storage.available_bytes == null ? "Unavailable" : `${(data.storage.available_bytes / 1073741824).toFixed(1)} GiB`]];
      document.querySelector("#status-list").replaceChildren(...rows.flatMap(([k,v]) => { const dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=k; dd.textContent=v; return [dt,dd]; }));
      await refreshSessions();
      await refreshDiagnostics();
      await refreshWebhooks();
      await refreshHeartbeatStatus();
      await loadAiRuntimeSettings();
      await loadBrandingSettings();
      try{const identity=await api("/api/auth/oidc/status");document.querySelector("#identity-provider-state").textContent=identity.enabled?"Microsoft Entra sign-in is enabled by deployment configuration.":"Microsoft Entra sign-in is not configured; local emergency sign-in remains available.";}catch(error){document.querySelector("#identity-provider-state").textContent="Identity provider status is unavailable: "+error.message;}
    }
    const inventory = await api("/api/cameras");
    latestCameras = inventory.cameras;
    sidebarCameras = inventory.cameras;
    const {groups} = await api("/api/camera-groups");
    sidebarGroups = groups;
    if (admin) await refreshGroups();
    await refreshIncidents();
    if(!document.querySelector('#camera-settings-content [data-config-dirty="true"]'))renderCameras(inventory.cameras);
    renderLiveCameraPicker(inventory.cameras);
    renderPlaybackCameraPicker(inventory.cameras,sidebarGroups);
    if(!playbackWorkspaceRestored){restorePlaybackWorkspace();playbackWorkspaceRestored=true;}
    await refreshLiveLayouts();
    if (!workspaceRestored) { await restoreLiveWorkspace(); workspaceRestored = true; }
    renderSidebar();
    await refreshExports();
    if (admin) await refreshDiscovery();
    await setScreen(["live","footage","camera-settings","app-settings"].includes(activeScreen)?activeScreen:"live");
    show("status");
  } catch (error) {
    if (error.message === "Authentication required" || error.message === "Session expired") {
      currentRole = null; await showLogin();
    } else { message.textContent = error.message; }
  }
}
function renderLiveCameraPicker(cameras) {
  const target=document.querySelector("#live-camera-picker");
  target.replaceChildren(...cameras.filter(camera=>!camera.archived_at).map(camera=>{
    const label=document.createElement("label"); label.textContent=`${camera.name}${camera.live_substream_available?" · low-resolution profile":""} `;
    const input=document.createElement("input"); input.type="checkbox"; input.dataset.liveCamera="true"; input.value=camera.id;
    input.checked=activeLiveCameraIds.includes(camera.id);
    input.addEventListener("change",updateLiveCameraSelection);
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
    updateOperationalSummary(result.incidents);
  } catch(error) { message.textContent=error.message; }
}
async function updateOperationalSummary(incidents=null){
  const summary=document.querySelector("#ops-status-summary"),count=document.querySelector("#ops-status-count");
  if(!summary||!count)return;
  if(incidents)latestActiveIncidentCount=incidents.filter(item=>item.state==="active").length;
  const warningList=document.querySelector("#ops-camera-warnings");
  const cameraProblems=latestCameras.filter(camera=>!camera.archived_at&&!(["recording","expected_idle"].includes(camera.recording_health?.state)));
  const storage=latestSystemStatus?.storage?.available_bytes;
  const storageText=currentRole==="administrator"&&Number.isFinite(storage)?` · ${(storage/1073741824).toFixed(1)} GiB database volume available`:"";
  const refreshText=latestOperationalRefreshAt?` Updated ${formatDateTime(latestOperationalRefreshAt)}.`:"";
  const errorText=operationalStatusError?" Camera status could not be refreshed; values shown are from the last successful check.":"";
  summary.textContent=`${latestCameras.filter(camera=>!camera.archived_at).length} active cameras · ${cameraProblems.length} recording or coverage warning(s) · ${latestActiveIncidentCount} active incident(s)${storageText}. Live video freshness and recording writes are tracked separately.${refreshText}${errorText}`;
  if(warningList){
    const rows=cameraProblems.slice(0,20).map(camera=>{
      const item=document.createElement("li"),detail=document.createElement("span"),button=document.createElement("button");
      const health=camera.recording_health||{};
      detail.textContent=`${camera.name} · ${health.state||"unknown"}${health.detail?` · ${health.detail}`:""}`;
      button.type="button";button.textContent="Inspect";button.setAttribute("aria-label",`Inspect ${camera.name} in the current workspace`);button.addEventListener("click",()=>inspectCameraFromStatus(camera.id));
      item.append(detail,button);return item;
    });
    if(cameraProblems.length>rows.length){const more=document.createElement("li");more.textContent=`${cameraProblems.length-rows.length} more warning(s). Search the camera inventory for a specific camera.`;rows.push(more);}
    if(!rows.length){const empty=document.createElement("li");empty.textContent="No camera recording or coverage warnings.";rows.push(empty);}
    warningList.replaceChildren(...rows);
  }
  const storageIssues=currentRole==="administrator"?(latestStorageStatus?.volumes||[]).filter(volume=>volume.state!=="healthy").length:0;
  const total=cameraProblems.length+latestActiveIncidentCount+latestExportProblemCount+storageIssues;
  count.textContent=String(Math.min(99,total));count.setAttribute("aria-label",`${total} open operational warning${total===1?"":"s"}`);
}
async function inspectCameraFromStatus(cameraId){
  if(activeScreen==="live"||activeScreen==="footage"){
    const current=currentWorkspaceCameraIds();
    if(current.includes(cameraId)){
      if(activeScreen==="live")startLiveViews(activeLiveCameraIds,latestCameras,cameraId,activeLiveGridSize,[...activeLiveTileCameraIds],activeLiveLayoutMode);
      else{const view=playbackViews.find(item=>item.cameraId===cameraId);if(view){playbackMaster=view;playbackViews.forEach(item=>{item.video.muted=item!==view;item.tile.classList.toggle("selected",item===view);});view.video.focus();}else if(!playbackTimeline)loadSynchronizedPlayback();}
      return;
    }
    const next=[...current,cameraId];
    if(next.length>16){const preferred=[...current.slice(Math.max(0,current.length-15)),cameraId];requestCameraSubset(next,preferred,chosen=>applySidebarCameraSelection(chosen));return;}
    applySidebarCameraSelection(next);
    if(activeScreen==="footage"&&!playbackTimeline)loadSynchronizedPlayback();
    return;
  }
  if(activeScreen==="camera-settings"&&selectedCameraId===cameraId){await setCameraWorkspace("cameras");mobileCameraOpen(cameraId);return;}
  if(activeScreen==="camera-settings"&&!await guardCameraDrafts())return;
  if(currentRole==="administrator"&&await setScreen("camera-settings")){
    if(!await setCameraWorkspace("cameras"))return;
    selectedCameraId=cameraId;cameraEditorState.selectCamera(cameraId);clearCameraHistory();mobileCameraEditorOpen=true;
    renderCameras(latestCameras);
    requestAnimationFrame(()=>document.querySelector(`#camera-inventory-table button[data-camera-id="${CSS.escape(cameraId)}"]`)?.focus());
  }
}
async function refreshOperationalStorage(){
  const section=document.querySelector("#ops-storage-section"),summary=document.querySelector("#ops-storage-summary"),target=document.querySelector("#ops-storage-list");
  if(currentRole!=="administrator"){section.classList.add("hidden");latestStorageStatus=null;return;}
  section.classList.remove("hidden");
  try{
    latestStorageStatus=await api("/api/storage");
    const volumes=latestStorageStatus.volumes||[];
    summary.textContent=volumes.length?`${volumes.filter(volume=>volume.state==="healthy").length} of ${volumes.length} recording volume(s) healthy.`:"No recording volumes are configured.";
    target.replaceChildren(...volumes.map(volume=>{const item=document.createElement("li"),free=Number.isFinite(volume.free_bytes)?` · ${(volume.free_bytes/1073741824).toFixed(1)} GiB free`:"";item.textContent=`${volume.id} · ${volume.state}${free}${volume.retention_at_risk?" · retention at risk":""}`;return item;}));
    if(!volumes.length){const item=document.createElement("li");item.textContent="No volumes reported.";target.append(item);}
    updateOperationalSummary();
  }catch(error){summary.textContent=`Storage status could not be refreshed: ${error.message}`;}
}
async function refreshOperationalInventory(){
  try{const result=await api("/api/cameras");latestCameras=result.cameras;sidebarCameras=result.cameras;operationalStatusError=false;latestOperationalRefreshAt=new Date();await updateOperationalSummary();if(currentRole==="administrator"&&!document.querySelector("#ops-status-drawer").classList.contains("hidden"))await refreshOperationalStorage();}
  catch{operationalStatusError=true;await updateOperationalSummary();}
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
function releaseLiveView(view) {
  view.controller.abort();
  if (view.url) URL.revokeObjectURL(view.url);
  const audio = view.tile && view.tile.querySelector("audio");
  if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
  if (view.tile) view.tile.remove();
}
function stopLiveViews() {
  if (liveStatusTimer) clearInterval(liveStatusTimer);
  liveStatusTimer = null;
  for (const view of liveViews) releaseLiveView(view);
  liveViews = [];
  activeLiveCameraIds = [];
  activeLiveTileCameraIds = [];
  activeLiveTileCameraId = null;
  focusedLiveCameraId = null;
  document.querySelector("#live-grid").replaceChildren();
}
function selectedLiveCameras() { return [...document.querySelectorAll("[data-live-camera]:checked")].map(input => input.value); }
function liveGridSizeFor(cameraCount) { return LIVE_GRID_SIZES.find(size => size >= cameraCount) || 16; }
function currentLiveTiles(size = activeLiveGridSize) {
  const tiles = [...document.querySelectorAll("#live-grid .live-tile")].map(tile => tile.dataset.cameraId || null);
  if (tiles.length === size) return tiles;
  return [...activeLiveCameraIds, ...Array(Math.max(0, size - activeLiveCameraIds.length)).fill(null)].slice(0, size);
}
function markLiveViewDirty() {
  liveViewDirty = true;
  document.querySelector("#live-unsaved").classList.remove("hidden");
  document.querySelector("#live-message").textContent = "Unsaved changes to this view.";
}
function clearLiveViewDirty() { liveViewDirty = false; document.querySelector("#live-unsaved").classList.add("hidden"); }
function requestCameraSubset(cameraIds, preferredIds, onApply) {
  const dialog = document.querySelector("#camera-subset-dialog");
  const list = dialog.querySelector("[data-subset-list]");
  const count = dialog.querySelector("[data-subset-count]");
  const chosen = new Set((preferredIds || []).slice(0, 16));
  list.replaceChildren();
  count.textContent = "Choose up to 16 cameras. Nothing is removed until you apply this selection.";
  for (const id of cameraIds) {
    const camera = latestCameras.find(item => item.id === id);
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = id;
    input.checked = chosen.has(id);
    input.dataset.subsetCamera = "";
    label.append(input, document.createTextNode(" " + (camera?.name || "Unavailable camera")));
    list.append(label);
  }
  dialog.querySelector("[data-subset-apply]").onclick = () => {
    const selected = [...dialog.querySelectorAll("[data-subset-camera]:checked")].map(input => input.value);
    if (selected.length > 16) { count.textContent = "Choose no more than 16 cameras."; return; }
    if (!selected.length) { count.textContent = "Choose at least one camera."; return; }
    dialog.close();
    onApply(selected);
  };
  dialog.querySelector("[data-subset-cancel]").onclick = () => dialog.close();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}
async function saveLiveWorkspace() {
  if (!currentRole) return;
  const arrangement = activeLiveTileCameraIds.length
    ? { tile_camera_ids: [...activeLiveTileCameraIds], grid_size: activeLiveGridSize, layout_mode: activeLiveLayoutMode }
    : cachedLiveArrangement || { tile_camera_ids: Array(activeLiveGridSize).fill(null), grid_size: activeLiveGridSize, layout_mode: activeLiveLayoutMode };
  const state = {
    camera_ids: arrangement.tile_camera_ids.filter(Boolean),
    tile_camera_ids: arrangement.tile_camera_ids,
    grid_size: arrangement.grid_size,
    layout_mode: arrangement.layout_mode || "auto",
    sidebar_collapsed: document.querySelector("#console-layout").classList.contains("sidebar-collapsed"),
  };
  clearTimeout(workspaceSaveTimer);
  workspaceSaveTimer = setTimeout(async () => {
    try { await api("/api/live/workspace", { method: "PUT", body: JSON.stringify(state) }); }
    catch (error) { document.querySelector("#live-message").textContent = "Workspace could not be saved: " + error.message; }
  }, 250);
}
async function restoreLiveWorkspace() {
  try {
    const state = await api("/api/live/workspace");
    const layout = document.querySelector("#console-layout");
    const button = document.querySelector("#sidebar-toggle");
    layout.classList.toggle("sidebar-collapsed", !!state.sidebar_collapsed);
    button.setAttribute("aria-expanded", String(!state.sidebar_collapsed));
    button.setAttribute("aria-label", state.sidebar_collapsed ? "Expand camera sidebar" : "Collapse camera sidebar");
    if (!state.camera_ids?.length) return;
    for (const input of document.querySelectorAll("[data-live-camera]")) input.checked = state.camera_ids.includes(input.value);
    document.querySelector("#live-grid-size").value = state.layout_mode === "auto" ? "auto" : String(state.grid_size);
    startLiveViews(state.camera_ids, latestCameras, null, state.grid_size, state.tile_camera_ids, state.layout_mode || "auto");
    clearLiveViewDirty();
  } catch (error) {
    document.querySelector("#live-message").textContent = "Previous workspace could not be restored: " + error.message;
  }
}
async function applySavedView(view) {
  if (activeScreen === "footage") {
    applySavedPlaybackView(view, view.camera_ids.filter(id => latestCameras.some(camera => camera.id === id && !camera.archived_at)));
    return;
  }
  if(!await setScreen("live"))return;
  document.querySelector("#live-layouts").value = view.scope + ":" + view.name;
  document.querySelector("#live-layout-name").value = view.name;
  for (const input of document.querySelectorAll("[data-live-camera]")) input.checked = view.camera_ids.includes(input.value);
  const mode = view.layout_mode || "auto";
  document.querySelector("#live-grid-size").value = mode === "auto" ? "auto" : String(view.grid_size);
  startLiveViews(view.camera_ids, latestCameras, null, view.grid_size, view.tile_camera_ids, mode);
  clearLiveViewDirty();
  document.querySelector("#live-message").textContent = "Applied " + view.scope + " view “" + view.name + "”.";
}
function applySavedPlaybackView(view, cameraIds) {
  const picker = document.querySelector("#playback-camera-picker");
  const labels = new Map([...picker.querySelectorAll("[data-playback-camera]")].map(input => [input.value, input.closest("label")]));
  const ordered = (view.tile_camera_ids || []).map(id => id && labels.get(id)).filter(Boolean);
  picker.querySelectorAll("fieldset")[1]?.prepend(...ordered);
  for (const input of picker.querySelectorAll("[data-playback-group]")) input.checked = false;
  playbackManualChoices.clear();
  for (const input of picker.querySelectorAll("[data-playback-camera]")) {
    input.checked = cameraIds.includes(input.value);
    playbackManualChoices.set(input.value, input.checked);
  }
  syncPlaybackGroupChecks();
  document.querySelector("#playback-grid-size").value = view.layout_mode === "auto" ? "auto" : String(view.grid_size);
  const unavailable = view.camera_ids.length - cameraIds.length;
  markPlaybackResultsStale();
  document.querySelector("#playback-status").textContent = "Applied " + view.scope + " view “" + view.name + "”. Camera selection and layout changed; date inputs were kept." + (unavailable ? " " + unavailable + " saved camera(s) are unavailable." : "");
  savePlaybackWorkspace();
  if(playbackTimeline)loadSynchronizedPlayback({preserve:true});
}
function desiredLiveQuality(focusedCameraId) {
  const choice = document.querySelector("#live-quality").value;
  return choice === "auto" ? (focusedCameraId ? "high" : "low") : choice;
}
function createLiveTile(cameraId, index, cameras, existingView) {
  const liveMessage = document.querySelector("#live-message");
  const camera = cameras.find(item => item.id === cameraId && !item.archived_at);
  const archived = cameras.find(item => item.id === cameraId && item.archived_at);
  const tile = document.createElement("article");
  tile.className = "live-tile";
  tile.tabIndex = 0;
  tile.dataset.tileIndex = String(index);
  tile.dataset.cameraId = cameraId;
  tile.draggable = Boolean(camera);
  tile.addEventListener("dragover", event => event.preventDefault());
  tile.addEventListener("drop", event => dropCameraOnTile(event, index, cameras));
  if (camera) tile.addEventListener("dragstart", event => event.dataTransfer.setData("text/plain", cameraId));
  tile.addEventListener("click", event => {
    if (event.target.closest("button,audio,select")) return;
    activeLiveTileCameraId = cameraId;
    document.querySelectorAll("#live-grid .live-tile").forEach(item => item.classList.toggle("active", item.dataset.cameraId === cameraId));
  });
  tile.addEventListener("dblclick", event => {
    if (event.target.closest("button,audio,select")) return;
    startLiveViews(activeLiveCameraIds, cameras, focusedLiveCameraId ? null : cameraId, activeLiveGridSize, [...activeLiveTileCameraIds], activeLiveLayoutMode);
  });
  const title = document.createElement("strong");
  title.textContent = camera?.name || (archived ? "Archived · " + archived.name : "Unavailable camera");
  const state = existingView?.state || document.createElement("span");
  state.className = "live-state";
  if (!existingView) state.textContent = camera ? "Loading" : archived ? "Archived" : "Unavailable";
  state.setAttribute("role", "status");
  state.setAttribute("aria-live", "polite");
  tile.append(title, state);
  if (!camera) return { tile, view: null };
  const image = existingView?.image || document.createElement("img");
  image.alt = "Live video from " + camera.name;
  image.dataset.cameraId=cameraId;
  const focus = document.createElement("button");
  focus.type = "button";
  focus.dataset.liveFocus = "";
  focus.textContent = focusedLiveCameraId ? "Restore grid" : "Focus";
  focus.setAttribute("aria-label", focusedLiveCameraId ? "Restore camera grid" : "Focus " + camera.name);
  focus.addEventListener("click", () => {
    activeLiveTileCameraId = cameraId;
    startLiveViews(activeLiveCameraIds, cameras, focusedLiveCameraId ? null : cameraId, activeLiveGridSize, [...activeLiveTileCameraIds], activeLiveLayoutMode);
  });
  const playback = document.createElement("button");
  playback.type = "button";
  playback.textContent = "Open in Playback";
  playback.addEventListener("click", () => openCameraInPlayback(cameraId));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.setAttribute("aria-label", "Remove " + camera.name + " from Live");
  remove.addEventListener("click", () => {
    const input = [...document.querySelectorAll("[data-live-camera]")].find(item => item.value === cameraId);
    if (input) input.checked = false;
    updateLiveCameraSelection();
  });
  const snapshot = document.createElement("button");
  snapshot.type = "button";
  snapshot.textContent = "Snapshot";
  snapshot.addEventListener("click", () => downloadLiveSnapshot(image, camera.name));
  tile.append(focus, snapshot, playback, remove, image);
  if (focusedLiveCameraId) {
    const fullscreen = document.createElement("button");
    fullscreen.type = "button";
    fullscreen.textContent = "Fullscreen";
    fullscreen.addEventListener("click", async () => {
      if (typeof tile.requestFullscreen !== "function") { liveMessage.textContent = "Fullscreen is not supported by this browser."; return; }
      try { await tile.requestFullscreen(); } catch { liveMessage.textContent = "The browser could not enter fullscreen."; }
    });
    tile.insertBefore(fullscreen, image);
  }
  const audio = existingView?.tile?.querySelector("audio") || null;
  const view = existingView || {
    cameraId, controller: new AbortController(), image, state, url: null, lastFrame: 0,
    startedAt: Date.now(), tile, quality: desiredLiveQuality(focusedLiveCameraId), retrying: false,
  };
  view.tile = tile;
  view.image = image;
  view.state = state;
  const retry=document.createElement("button");retry.type="button";retry.textContent="Retry";retry.setAttribute("aria-label","Retry live video for "+camera.name);
  retry.addEventListener("click",()=>{
    view.controller.abort();view.controller=new AbortController();view.startedAt=Date.now();view.retrying=true;
    view.state.textContent=view.lastFrame?`Stale · last frame ${new Date(view.lastFrame).toLocaleTimeString()}`:"Reconnecting";
    consumeLiveStream(view).finally(()=>{view.retrying=false;});
  });
  tile.insertBefore(retry,image);
  if (camera.audio_enabled) {
    const listening = audio || document.createElement("audio");
    listening.controls = true;
    listening.preload = "none";
    listening.setAttribute("aria-label", "Live audio from " + camera.name);
    listening.muted = audio ? audio.muted : true;
    listening.title = "Audio is muted. Only one camera can play audio at a time.";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = listening.muted ? "Listen" : "Mute audio";
    toggle.setAttribute("aria-label", (listening.muted ? "Listen to " : "Mute audio from ") + camera.name);
    toggle.addEventListener("click", async () => {
      listening.muted = !listening.muted;
      if (listening.muted) listening.pause();
      else {
        for (const other of document.querySelectorAll("#live-grid audio")) if (other !== listening) { other.pause(); other.muted = true; }
        try { await listening.play(); } catch { listening.muted = true; liveMessage.textContent = "Audio could not start for " + camera.name + "."; }
      }
      toggle.textContent = listening.muted ? "Listen" : "Mute audio";
    });
    if (!audio) listening.src = "/api/cameras/" + encodeURIComponent(camera.id) + "/audio/live";
    tile.insertBefore(toggle, image);
    tile.append(listening);
  }
  return { tile, view };
}
function downloadLiveSnapshot(image, cameraName) {
  if (!image.naturalWidth) { document.querySelector("#live-message").textContent = "Wait for a live frame before taking a snapshot."; return; }
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  canvas.getContext("2d").drawImage(image, 0, 0);
  canvas.toBlob(blob => {
    if (!blob) return;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = cameraName.replace(/[^a-z0-9_-]+/gi, "-") + "-snapshot.jpg";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }, "image/jpeg", 0.92);
}
function updateLiveGridLayout() {
  const grid = document.querySelector("#live-grid");
  if (!grid) return;
  const width = grid.clientWidth || window.innerWidth;
  const columns = focusedLiveCameraId ? 1 : activeLiveLayoutMode === "auto"
    ? autoLayoutForCount(activeLiveCameraIds.length, width).columns
    : Math.ceil(Math.sqrt(activeLiveGridSize));
  grid.style.setProperty("--live-grid-columns", String(columns));
  grid.querySelectorAll(".live-tile").forEach(tile => {
    tile.hidden = Boolean(focusedLiveCameraId && tile.dataset.cameraId !== focusedLiveCameraId);
    tile.classList.toggle("active", tile.dataset.cameraId === activeLiveTileCameraId);
  });
}
function startLiveViews(cameraIds, cameras, focusedCameraId = null, gridSize = null, tileCameraIds = null, layoutMode = null) {
  const mode = layoutMode || (document.querySelector("#live-grid-size").value === "auto" ? "auto" : "manual");
  const requested = new Set(cameraIds);
  const ordered = tileCameraIds ? [...new Set(tileCameraIds.filter(Boolean).filter(id => requested.has(id)))] : [];
  for (const id of cameraIds) if (!ordered.includes(id)) ordered.push(id);
  if (ordered.length > 16) {
    requestCameraSubset(ordered, activeLiveCameraIds, chosen => startLiveViews(chosen, cameras, null, null, chosen, mode));
    return;
  }
  if (!ordered.length) {
    stopLiveViews();
    activeLiveLayoutMode = mode;
    activeLiveGridSize = mode === "auto" ? 1 : Number(gridSize) || 4;
    cachedLiveArrangement = { camera_ids: [], tile_camera_ids: [], grid_size: activeLiveGridSize, layout_mode: mode };
    document.querySelector("#live-message").textContent = "Select a camera to start Live viewing.";
    saveLiveWorkspace();
    return;
  }
  const selectedSize = mode === "auto" ? liveGridSizeFor(ordered.length) : Number(gridSize) || Number(document.querySelector("#live-grid-size").value) || 4;
  if (ordered.length > selectedSize) {
    document.querySelector("#live-grid-size").value = "auto";
    document.querySelector("#live-message").textContent = "Auto layout restored to keep every selected camera visible.";
    return startLiveViews(ordered, cameras, focusedCameraId, null, null, "auto");
  }
  const tiles = [...ordered, ...Array(Math.max(0, selectedSize - ordered.length)).fill(null)].slice(0, selectedSize);
  const previous = new Map(liveViews.map(view => [view.cameraId, view]));
  for (const view of liveViews) if (!ordered.includes(view.cameraId)) releaseLiveView(view);
  activeLiveCameraIds = [...ordered];
  activeLiveTileCameraIds = tiles;
  activeLiveGridSize = selectedSize;
  activeLiveLayoutMode = mode;
  focusedLiveCameraId = focusedCameraId && ordered.includes(focusedCameraId) ? focusedCameraId : null;
  if (!activeLiveTileCameraId || !ordered.includes(activeLiveTileCameraId)) activeLiveTileCameraId = focusedLiveCameraId || ordered[0];
  cachedLiveArrangement = { camera_ids: [...ordered], tile_camera_ids: [...tiles], grid_size: selectedSize, layout_mode: mode };
  document.querySelector("#live-grid-size").value = mode === "auto" ? "auto" : String(selectedSize);
  for (const input of document.querySelectorAll("[data-live-camera]")) input.checked = ordered.includes(input.value);
  const grid = document.querySelector("#live-grid");
  grid.replaceChildren();
  const slots = mode === "auto" ? ordered.length : selectedSize;
  const nextViews = [];
  for (let index = 0; index < slots; index++) {
    const cameraId = tiles[index] || null;
    if (!cameraId) {
      const tile = document.createElement("article");
      tile.className = "live-tile live-placeholder";
      tile.dataset.tileIndex = String(index);
      tile.textContent = "Empty tile · drop a camera here";
      tile.addEventListener("dragover", event => event.preventDefault());
      tile.addEventListener("drop", event => dropCameraOnTile(event, index, cameras));
      grid.append(tile);
      continue;
    }
    const { tile, view } = createLiveTile(cameraId, index, cameras, previous.get(cameraId));
    grid.append(tile);
    if (view) {
      const quality = desiredLiveQuality(focusedLiveCameraId);
      if (view.quality !== quality) {
        view.controller.abort();
        view.controller = new AbortController();
        view.quality = quality;
        view.startedAt = Date.now();
        consumeLiveStream(view).catch(error => {
          if (!view.controller.signal.aborted) { view.state.textContent = "Lost"; document.querySelector("#live-message").textContent = error.message; }
        });
      } else if (!previous.has(cameraId)) {
        consumeLiveStream(view).catch(error => {
          if (!view.controller.signal.aborted) { view.state.textContent = "Lost"; document.querySelector("#live-message").textContent = error.message; }
        });
      }
      nextViews.push(view);
    }
  }
  liveViews = nextViews;
  if (liveStatusTimer) clearInterval(liveStatusTimer);
  liveStatusTimer = setInterval(async () => {
    for (const view of liveViews) {
      if (view.controller.signal.aborted) continue;
      if (Date.now() - (view.lastFrame || view.startedAt) > 12000 && !view.retrying) {
        view.retrying = true;
        view.state.textContent = view.lastFrame ? `Stale · last frame ${new Date(view.lastFrame).toLocaleTimeString()}` : "Reconnecting";
        view.tile.classList.toggle("stale",Boolean(view.lastFrame));
        view.controller.abort();
        view.controller = new AbortController();
        view.lastFrame = 0;
        view.startedAt = Date.now();
        consumeLiveStream(view).finally(() => { view.retrying = false; });
        continue;
      }
      try {
        const status = await api("/api/cameras/" + encodeURIComponent(view.cameraId) + "/live/status");
        if (!view.controller.signal.aborted) view.state.textContent = view.lastFrame
          ? Date.now() - view.lastFrame > 12000 ? "Stale · reconnecting" : status.state === "live" ? "Live" : status.active_viewers ? "Stale · reconnecting" : "Reconnecting"
          : status.state === "stale" ? "Stale · reconnecting" : "Loading";
      } catch { if (!view.controller.signal.aborted) view.state.textContent = view.lastFrame ? "Stale · reconnecting" : "Lost"; }
    }
  }, 5000);
  updateLiveGridLayout();
  markLiveViewDirty();
  saveLiveWorkspace();
  updateOperationalSummary();
}
function updateLiveCameraSelection() {
  const selected = selectedLiveCameras();
  if (selected.length > 16) {
    requestCameraSubset(selected, activeLiveCameraIds, chosen => {
      for (const input of document.querySelectorAll("[data-live-camera]")) input.checked = chosen.includes(input.value);
      const ordered = reconcileCameraOrder(activeLiveCameraIds, chosen);
      const mode = document.querySelector("#live-grid-size").value === "auto" ? "auto" : "manual";
      startLiveViews(ordered, latestCameras, null, null, null, mode);
    });
    return;
  }
  const order = reconcileCameraOrder(activeLiveCameraIds, selected);
  const mode = document.querySelector("#live-grid-size").value === "auto" ? "auto" : "manual";
  const capacity = mode === "auto" ? liveGridSizeFor(order.length) : Number(document.querySelector("#live-grid-size").value);
  startLiveViews(order, latestCameras, focusedLiveCameraId, capacity, activeLiveTileCameraIds, mode);
  renderSidebar();
}
function openCameraInPlayback(cameraId) {
  const input = [...document.querySelectorAll("[data-playback-camera]")].find(item => item.value === cameraId);
  if (!input) return;
  const chosen = new Set([...document.querySelectorAll("[data-playback-camera]:checked")].map(item => item.value));
  chosen.add(cameraId);
  const apply = async ids => {
    if(!await setScreen("footage"))return;
    for (const item of document.querySelectorAll("[data-playback-camera]")) item.checked = ids.includes(item.value);
    for (const id of ids) playbackManualChoices.set(id, true);
    syncPlaybackGroupChecks();
    markPlaybackResultsStale();
    savePlaybackWorkspace();
    document.querySelector("#playback-search").focus();
  };
  if (chosen.size > 16) requestCameraSubset([...chosen], [...chosen].slice(0, 16), apply);
  else apply([...chosen]);
}
function waitForLiveRetry(delay,signal){return new Promise(resolve=>{const finish=()=>{clearTimeout(timer);signal.removeEventListener("abort",finish);resolve();};const timer=setTimeout(finish,delay);signal.addEventListener("abort",finish,{once:true});});}
async function consumeLiveStream(view) {
  let attempt=0;
  while(!view.controller.signal.aborted){
    let retryable=true;
    try{
      view.state.textContent=view.lastFrame?`Stale · last frame ${new Date(view.lastFrame).toLocaleTimeString()}`:"Loading";
      view.tile.classList.toggle("stale",Boolean(view.lastFrame));
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
          if(view.url)URL.revokeObjectURL(view.url);view.url=url;view.image.src=url;view.lastFrame=Date.now();view.startedAt=view.lastFrame;view.retrying=false;view.state.textContent="Live";view.tile.classList.remove("stale");
        }
        if(buffer.length>4*1024*1024)throw new Error("Live stream exceeded the frame buffer limit");
      }
      return;
    }catch(error){
      if(view.controller.signal.aborted)return;
      if(!retryable){view.retrying=false;view.state.textContent="Lost";return;}
      view.state.textContent="Reconnecting";
      if(view.url)view.state.textContent=`Stale · last frame ${new Date(view.lastFrame).toLocaleTimeString()}`;
      view.tile.classList.toggle("stale",Boolean(view.url));
      await waitForLiveRetry(Math.min(1000*2**attempt,8000),view.controller.signal);
      attempt=Math.min(attempt+1,4);
    }
  }
}
function dropCameraOnTile(event,index,cameras){
  event.preventDefault();const cameraId=event.dataTransfer.getData("text/plain");if(!cameraId)return;
  const next=[...activeLiveCameraIds],current=next.indexOf(cameraId);
  if(current>=0){if(index>=next.length)return;[next[current],next[index]]=[next[index],next[current]];startLiveViews(next,cameras,focusedLiveCameraId,activeLiveGridSize,[...next],activeLiveLayoutMode);return;}
  if(activeLiveLayoutMode==="auto")next.splice(Math.min(index,next.length),0,cameraId);
  else if(next.length>=activeLiveGridSize){
    const replaced=next[index];if(!replaced){document.querySelector("#live-message").textContent="This manual grid has no empty tile to replace.";return;}
    next[index]=cameraId;
    const oldInput=[...document.querySelectorAll("[data-live-camera]")].find(item=>item.value===replaced);if(oldInput)oldInput.checked=false;
  } else next.splice(Math.min(index,next.length),0,cameraId);
  const input=[...document.querySelectorAll("[data-live-camera]")].find(item=>item.value===cameraId);if(input)input.checked=true;
  startLiveViews(next,cameras,null,activeLiveGridSize,[...next],activeLiveLayoutMode);
  renderSidebar();
}
function addCameraToLiveGrid(cameraId,cameras){
  if(activeLiveCameraIds.includes(cameraId))return;
  if(document.querySelector("#live-grid-size").value==="auto"){
    const next=[...activeLiveCameraIds,cameraId];
    const choice=[...document.querySelectorAll("[data-live-camera]")].find(item=>item.value===cameraId);if(choice)choice.checked=true;
    startLiveViews(next,cameras,null,null,null,"auto");return;
  }
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
    const label=document.createElement("label"); label.textContent=`${camera.name}${camera.archived_at?" · archived":""} `;
    const input=document.createElement("input"); input.type="checkbox"; input.dataset.playbackCamera="true"; input.value=camera.id;
    label.append(input); return label;
  }));
  target.replaceChildren(groupFieldset,cameraFieldset);
  syncPlaybackCameraChecks();
}
function playbackWorkspaceKey(){return `nvr-playback-workspace:${currentUsername}`;}
function savePlaybackWorkspace(){
  if(!currentUsername)return;
  try{
    localStorage.setItem(playbackWorkspaceKey(),JSON.stringify({
      camera_ids:[...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value),
      start:document.querySelector("#playback-start").value,end:document.querySelector("#playback-end").value,
      start_fold:document.querySelector("#playback-start-fold").value,end_fold:document.querySelector("#playback-end-fold").value,
      grid_size:document.querySelector("#playback-grid-size").value,
      export_start:document.querySelector("#export-start").value,export_end:document.querySelector("#export-end").value,
      export_start_fold:document.querySelector("#export-start-fold").value,export_end_fold:document.querySelector("#export-end-fold").value,
      export_camera_ids:[...document.querySelectorAll("[data-export-camera]:checked")].map(input=>input.value),
      time:playbackTimeline?document.querySelector("#playback-cursor").dataset.time:null,
      playing:playbackIsPlaying,
    }));
  }catch{}
}
function schedulePlaybackWorkspaceSave(){
  if(playbackWorkspaceSaveTimer)clearTimeout(playbackWorkspaceSaveTimer);
  playbackWorkspaceSaveTimer=setTimeout(()=>{playbackWorkspaceSaveTimer=null;savePlaybackWorkspace();},750);
}
function restorePlaybackWorkspace(){
  try{
    const saved=JSON.parse(localStorage.getItem(playbackWorkspaceKey())||"null");
    if(!saved||typeof saved!=="object")return;
    const available=new Set([...document.querySelectorAll("[data-playback-camera]")].map(input=>input.value));
    const chosen=new Set((saved.camera_ids||[]).filter(id=>available.has(id)));
    for(const input of document.querySelectorAll("[data-playback-camera]")){input.checked=chosen.has(input.value);playbackManualChoices.set(input.value,input.checked);}
    if(saved.start)document.querySelector("#playback-start").value=saved.start;
    if(saved.end)document.querySelector("#playback-end").value=saved.end;
    updateFoldChoices("start");updateFoldChoices("end");
    if(saved.start_fold)document.querySelector("#playback-start-fold").value=saved.start_fold;
    if(saved.end_fold)document.querySelector("#playback-end-fold").value=saved.end_fold;
    if(["auto","1","4","9","16"].includes(saved.grid_size))document.querySelector("#playback-grid-size").value=saved.grid_size;
    if(saved.export_start)document.querySelector("#export-start").value=saved.export_start;
    if(saved.export_end)document.querySelector("#export-end").value=saved.export_end;
    updateExportFoldChoices("start");updateExportFoldChoices("end");
    if(saved.export_start_fold)document.querySelector("#export-start-fold").value=saved.export_start_fold;
    if(saved.export_end_fold)document.querySelector("#export-end-fold").value=saved.export_end_fold;
    pendingPlaybackRestoreState=saved;
    syncPlaybackGroupChecks();updatePlaybackGridLayout(chosen.size);
  }catch{}
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
  updatePlaybackExportPreview();
}
function stopSynchronizedPlayback() {
  stopPlaybackClock();
  playbackViews.forEach(view=>{view.video.pause();view.video.removeAttribute("src");view.video.load();});
  playbackViews=[]; playbackTimeline=null; playbackMaster=null;playbackWindow=null;
  playbackExpandedView=null;
  document.querySelector("#playback-grid")?.classList.remove("expanded");
  document.querySelector("#playback-grid")?.replaceChildren();
  const cursor=document.querySelector("#playback-cursor"); if(cursor) cursor.disabled=true;
  ["#playback-play","#playback-pause","#playback-prev-frame","#playback-next-frame","#playback-prev-event","#playback-next-event"].forEach(selector=>{const button=document.querySelector(selector);if(button)button.disabled=true;});
}
function stopPlaybackClock() { if(playbackClockTimer){clearInterval(playbackClockTimer);playbackClockTimer=null;} playbackIsPlaying=false;playbackClockAnchor=null; }
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
  updateExportSelectionOverlay(track);
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
function playbackStartVideo(view) {
  view.programmaticPlay=true;
  view.video.play().catch(()=>{view.programmaticPlay=false;view.state.textContent="Press Play to start";});
}
function playbackPauseVideo(view) {
  if(view.video.paused)return;
  view.programmaticPause=true;
  view.video.pause();
}
function playbackSetSegment(view,timeMs,playing=false) {
  const segment=playbackSegmentAt(view,timeMs);
  if(!segment) {
    if(view.segment){playbackPauseVideo(view);view.video.removeAttribute("src");view.video.load();view.segment=null;}
    view.state.textContent="No footage at this time";
    return false;
  }
  const changed=!view.segment||view.segment.id!==segment.id;
  view.segment=segment; view.targetMs=timeMs;
  const offset=Math.max(0,(timeMs-playbackMilliseconds(segment.starts_at))/1000);
  if(changed) {
    playbackPauseVideo(view);
    view.video.src=segment.media_url; view.video.load(); view.state.textContent="Loading interval…";
    view.video.onloadedmetadata=()=>{if(!view.video.isConnected)return;view.video.currentTime=Math.min(offset,Math.max(0,view.video.duration-0.05));view.video.playbackRate=Number(document.querySelector("#playback-rate").value);view.state.textContent="Ready";if(playing)playbackStartVideo(view);};
  } else {
    const drift=Math.abs(view.video.currentTime-offset);
    if(drift>0.3)view.video.currentTime=offset;
    view.video.playbackRate=Number(document.querySelector("#playback-rate").value);
    if(playing&&view.video.paused)playbackStartVideo(view);
    if(!playing)playbackPauseVideo(view);
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
  schedulePlaybackWorkspaceSave();
}
function playbackHasFootageAt(timeMs) { return playbackViews.some(view=>Boolean(playbackSegmentAt(view,timeMs))); }
function nextPlaybackFootage(timeMs) {
  const starts=playbackViews.flatMap(view=>view.segments.map(segment=>playbackMilliseconds(segment.starts_at))).filter(value=>value>timeMs).sort((a,b)=>a-b);
  return starts.find(value=>playbackHasFootageAt(value))??null;
}
function startPlaybackClock() {
  if(!playbackTimeline)return;
  if(playbackClockTimer)clearInterval(playbackClockTimer);
  playbackMaster=selectedPlaybackView();playbackViews.forEach(view=>{view.video.muted=view!==playbackMaster;});
  playbackIsPlaying=true;
  const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start);
  playbackClockAnchor={at,wall:performance.now()};
  playbackViews.forEach(view=>{view.video.playbackRate=Number(document.querySelector("#playback-rate").value);playbackPauseVideo(view);});
  seekSynchronizedPlayback(at,true,playbackMaster);
  playbackClockTimer=setInterval(()=>{
    if(!playbackClockAnchor||!playbackTimeline)return;
    const rate=Number(document.querySelector("#playback-rate").value),candidate=playbackClockAnchor.at+(performance.now()-playbackClockAnchor.wall)*rate;
    const end=playbackMilliseconds(playbackTimeline.end);
    if(candidate>=end){seekSynchronizedPlayback(end,false);stopPlaybackClock();return;}
    let target=candidate;
    if(document.querySelector("#playback-skip-empty").checked&&!playbackHasFootageAt(target)){
      const next=nextPlaybackFootage(target);
      if(next===null){seekSynchronizedPlayback(end,false);stopPlaybackClock();return;}
      target=next;playbackClockAnchor={at:target,wall:performance.now()};
    }
    seekSynchronizedPlayback(target,true,playbackMaster);
  },100);
}
function pausePlaybackClock() {
  if(playbackClockAnchor&&playbackIsPlaying){const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackClockAnchor.at);playbackClockAnchor={at,wall:performance.now()};}
  stopPlaybackClock();playbackViews.forEach(playbackPauseVideo);savePlaybackWorkspace();
}
function selectedPlaybackView() { return playbackMaster&&playbackViews.includes(playbackMaster)?playbackMaster:playbackViews[0]||null; }
async function stepPlaybackFrame(direction) {
  if(!playbackTimeline)return;
  pausePlaybackClock();
  const view=selectedPlaybackView();if(!view)return;
  const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start);
  const segments=[...view.segments].sort((a,b)=>playbackMilliseconds(a.starts_at)-playbackMilliseconds(b.starts_at));
  const ordered=direction==="previous"?segments.filter(segment=>playbackMilliseconds(segment.starts_at)<at).reverse().concat(segments.filter(segment=>playbackMilliseconds(segment.starts_at)<=at&&playbackMilliseconds(segment.ends_at)>at).reverse()):segments.filter(segment=>playbackMilliseconds(segment.ends_at)>at);
  let best=null;
  for(const segment of ordered){
    const start=playbackMilliseconds(segment.starts_at),end=playbackMilliseconds(segment.ends_at);
    const requested=Math.max(start,Math.min(end-1,at));
    try {
      const result=await api(`/api/cameras/${encodeURIComponent(view.cameraId)}/segments/${segment.id}/frame-neighbor?at=${encodeURIComponent(new Date(requested).toISOString())}&direction=${direction}`);
      const found=playbackMilliseconds(result.at);if((direction==="previous"&&found<at||direction==="next"&&found>at)&&(!best||(direction==="previous"?found>best:found<best)))best=found;
    }catch(error){if(!["No adjacent frame exists in this interval","Frame time is outside this recorded interval"].includes(error.message))throw error;}
    if(best!==null)break;
  }
  if(best===null){document.querySelector("#playback-status").textContent=`No ${direction} frame is available for ${view.name} within the searched range.`;return;}
  const rangeStart=playbackMilliseconds(playbackTimeline.start),rangeEnd=playbackMilliseconds(playbackTimeline.end);
  if(best<rangeStart||best>=rangeEnd){document.querySelector("#playback-status").textContent=`No ${direction} frame is available within the searched range.`;return;}
  seekSynchronizedPlayback(best,false,view);
}
function navigatePlaybackEvent(direction) {
  if(!playbackTimeline)return;
  const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start),scope=document.querySelector("#playback-event-scope").value;
  const views=scope==="selected"?[selectedPlaybackView()].filter(Boolean):playbackViews;
  const times=[...new Set(views.flatMap(view=>view.motion_events.map(event=>playbackMilliseconds(event.source_event_at))).filter(value=>value>=playbackMilliseconds(playbackTimeline.start)&&value<playbackMilliseconds(playbackTimeline.end)))].sort((a,b)=>a-b);
  const destination=direction==="next"?times.find(value=>value>at):times.filter(value=>value<at).pop();
  if(destination===undefined){document.querySelector("#playback-status").textContent=`No ${direction} event within the searched range.`;return;}
  const playing=playbackIsPlaying;seekSynchronizedPlayback(destination,playing);if(playing)startPlaybackClock();
}
function expandPlaybackView(view) {
  const grid=document.querySelector("#playback-grid");
  if(playbackExpandedView===view){playbackExpandedView=null;grid.classList.remove("expanded");playbackViews.forEach(item=>{item.tile.classList.remove("hidden","expanded");item.expandButton.textContent="Expand";});return;}
  playbackExpandedView=view;grid.classList.add("expanded");playbackViews.forEach(item=>item.tile.classList.toggle("hidden",item!==view));view.tile.classList.add("expanded");view.expandButton.textContent="Restore grid";
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
  const select=document.createElement("button");select.type="button";select.textContent="Select camera";select.addEventListener("click",()=>{playbackMaster=view;playbackViews.forEach(item=>{item.video.muted=item!==view;item.tile.classList.toggle("selected",item===view);});});
  const snapshot=document.createElement("button"); snapshot.type="button"; snapshot.textContent="Download snapshot";
  snapshot.addEventListener("click",async()=>{
    if(playbackResultsStale){state.textContent="Search inputs changed. Reload the timeline before capturing a snapshot.";return;}
    const at=Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start);
    const segment=playbackSegmentAt(view,at);
    if(!segment){state.textContent="No snapshot is available at the shared time.";return;}
    try {const response=await fetch(`${segment.media_url.replace(/\/media$/,"")}/snapshot?at=${encodeURIComponent(new Date(at).toISOString())}`,{credentials:"same-origin"});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||"Snapshot is unavailable");}const sourceTime=response.headers.get("X-Source-Time"),audit=response.headers.get("X-Audit-Reference"),blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=`${view.name.replace(/[^a-z0-9_-]+/gi,"_")}-${new Date(at).toISOString().replace(/[:.]/g,"-")}.jpg`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);state.textContent=`Snapshot downloaded from ${sourceTime||new Date(at).toISOString()} · audit ${audit||"reference unavailable"}.`;}catch(error){state.textContent=error.message;}
  });
  const fullscreen=document.createElement("button"); fullscreen.type="button"; fullscreen.textContent="Fullscreen";
  fullscreen.addEventListener("click",()=>{if(!tile.requestFullscreen){state.textContent="Fullscreen is not supported by this browser.";return;}tile.requestFullscreen().catch(()=>{state.textContent="Fullscreen was blocked by the browser.";});});
  const expand=document.createElement("button");expand.type="button";expand.textContent="Expand";expand.addEventListener("click",()=>expandPlaybackView(view));
  controls.append(select,snapshot,fullscreen,expand); tile.append(heading,state,video,controls);
  const track=renderPlaybackTrack(view),motionList=renderPlaybackMotionList(view);
  tile.append(track);
  tile.append(motionList);
  view.video=video;view.state=state;view.tile=tile;view.expandButton=expand;
  video.addEventListener("play",()=>{if(view.programmaticPlay){view.programmaticPlay=false;return;}if(playbackSyncGuard)return;playbackMaster=view;playbackViews.forEach(other=>{other.video.muted=other!==view;});startPlaybackClock();});
  video.addEventListener("pause",()=>{
    if(view.programmaticPause){view.programmaticPause=false;return;}
    const reachedSegmentEnd=video.ended||(Number.isFinite(video.duration)&&video.currentTime>=video.duration-0.05);
    if(playbackSyncGuard||reachedSegmentEnd)return;
    pausePlaybackClock();
  });
  video.addEventListener("seeking",()=>{if(playbackSyncGuard)return;const time=playbackAbsTime(view);if(time!==null){playbackMaster=view;seekSynchronizedPlayback(time,!view.video.paused,view);if(playbackIsPlaying)startPlaybackClock();}});
  video.addEventListener("timeupdate",()=>{if(playbackSyncGuard||playbackMaster!==view||!playbackIsPlaying)return;updatePlaybackDrift();});
  return tile;
}
async function loadSynchronizedPlayback(options={}) {
  const camera_ids=[...new Set([...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value))];
  const restoring=options.preserve?null:pendingPlaybackRestoreState;
  const savedTime=Number(restoring?.time);
  const previousTime=options.preserve&&playbackTimeline?Number(document.querySelector("#playback-cursor").dataset.time):Number.isFinite(savedTime)&&savedTime>0?savedTime:null;
  const wasPlaying=Boolean(options.preserve&&playbackIsPlaying);
  const previousMarks=options.preserve?{start:document.querySelector("#export-start").value,end:document.querySelector("#export-end").value,
    startFold:document.querySelector("#export-start-fold").value,endFold:document.querySelector("#export-end-fold").value,
    cameras:[...document.querySelectorAll("[data-export-camera]:checked")].map(input=>input.value)}:restoring?{
    start:restoring.export_start,end:restoring.export_end,startFold:restoring.export_start_fold,endFold:restoring.export_end_fold,
    cameras:restoring.export_camera_ids||[]}:null;
  const startInput=document.querySelector("#playback-start").value,endInput=document.querySelector("#playback-end").value;
  const label=document.querySelector("#playback-status");
  try {
    if(!camera_ids.length||camera_ids.length>16)throw new Error("Choose between one and sixteen unique cameras.");
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
    playbackViews=result.cameras.map(camera=>({cameraId:camera.camera_id,name:camera.name,audio_supported:camera.audio_supported,audio_enabled:camera.audio_enabled,segments:camera.segments,gaps:camera.gaps,motion_events:camera.motion_events,coverage:camera.coverage,video:null,state:null,segment:null}));
    updatePlaybackGridLayout(playbackViews.length);
    grid.replaceChildren(...playbackViews.map(renderPlaybackCamera));
    renderPlaybackRuler();
    const earliest=playbackViews.flatMap(view=>view.segments.map(segment=>playbackMilliseconds(segment.starts_at))).filter(Number.isFinite);
    const firstAvailable=earliest.length?Math.min(...earliest):playbackMilliseconds(result.start);
    const seekTarget=previousTime===null?firstAvailable:Math.min(playbackMilliseconds(result.end),Math.max(playbackMilliseconds(result.start),previousTime));
    seekSynchronizedPlayback(seekTarget);
    if(playbackViews.length){playbackMaster=playbackViews[0];playbackViews[0].tile.classList.add("selected");playbackViews.forEach((view,index)=>{view.video.muted=index>0;});}
    ["#playback-play","#playback-pause","#playback-prev-frame","#playback-next-frame","#playback-prev-event","#playback-next-event"].forEach(selector=>{document.querySelector(selector).disabled=false;});
    playbackResultsStale=false;document.querySelector("#playback-stale").classList.add("hidden");
    initializeExportSelection(previousMarks?.cameras);
    const markStart=previousMarks?.start?storedLocalInstant(previousMarks.start,previousMarks.startFold):null;
    const markEnd=previousMarks?.end?storedLocalInstant(previousMarks.end,previousMarks.endFold):null;
    setExportBoundary("start",markStart??playbackMilliseconds(result.start));
    setExportBoundary("end",markEnd??playbackMilliseconds(result.end));
    if(previousMarks?.startFold)document.querySelector("#export-start-fold").value=previousMarks.startFold;
    if(previousMarks?.endFold)document.querySelector("#export-end-fold").value=previousMarks.endFold;
    pendingPlaybackRestoreState=null;
    updatePlaybackExportPreview();savePlaybackWorkspace();
    label.textContent=`Loaded ${result.cameras.length} camera timeline(s) at ${formatDateTime(seekTarget)}. Events and gaps are aligned to the same search range.`;
    if(wasPlaying)startPlaybackClock();
  }catch(error){label.textContent=error.message;}
}
async function refreshExports() {
  const target=document.querySelector("#export-jobs");
  if(!target)return;
  try {
    const {exports}=await api("/api/exports");
    const activeJobs=exports.filter(job=>job.status==="queued"||job.status==="running").length;
    const failedJobs=exports.filter(job=>job.status==="failed").length;
    latestExportProblemCount=failedJobs;
    const completedJobs=exports.filter(job=>job.status==="completed").length;
    document.querySelector("#ops-job-summary").textContent=`Exports: ${activeJobs} active · ${failedJobs} failed · ${completedJobs} completed.`;
    updateOperationalSummary();
    target.replaceChildren(...exports.map(job=>{
      const row=document.createElement("li");
      const state=document.createElement("span");
      state.textContent=`${job.id.slice(0,8)} · ${job.status}${job.status==="queued"||job.status==="running"?` · ${job.progress}%`:""} · ${formatDateTime(job.start)} – ${formatDateTime(job.end)}${job.expires_at?` · expires ${formatDateTime(job.expires_at)}`:""}${job.cancel_requested?" · cancelling":""}${job.error?` · ${job.error}`:""}`;
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
const WEEKDAY_LABELS=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
function addScheduleInterval(editor,interval={days:[0,1,2,3,4],start:"08:00",end:"17:00",mode:"continuous"}){
  const row=document.createElement("fieldset");row.className="schedule-interval";
  const legend=document.createElement("legend");legend.textContent=`Interval ${editor.querySelectorAll(".schedule-interval").length+1}`;row.append(legend);
  const days=document.createElement("div");days.className="schedule-days";days.setAttribute("aria-label","Days this interval applies");
  WEEKDAY_LABELS.forEach((label,index)=>{const dayLabel=document.createElement("label"),input=document.createElement("input");input.type="checkbox";input.value=String(index);input.checked=(interval.days||[]).includes(index);dayLabel.append(input,document.createTextNode(label));days.append(dayLabel);});
  const timeRow=document.createElement("div");timeRow.className="schedule-times";
  for(const [field,label] of [["start","From"],["end","To"]]){const item=document.createElement("label");item.textContent=label;const input=document.createElement("input");input.type="time";input.required=true;input.value=interval[field]||"08:00";input.dataset.scheduleField=field;item.append(input);timeRow.append(item);}
  const modeLabel=document.createElement("label");modeLabel.textContent="Recording mode";const mode=document.createElement("select");mode.dataset.scheduleField="mode";for(const [value,name] of [["continuous","Continuous"],["motion","Motion"],["disabled","Disabled"]])mode.add(new Option(name,value));mode.value=interval.mode||"continuous";modeLabel.append(mode);
  const remove=document.createElement("button");remove.type="button";remove.textContent="Remove interval";remove.addEventListener("click",()=>{markConfigDraftElement(editor);row.remove();[...editor.querySelectorAll(".schedule-interval legend")].forEach((item,index)=>item.textContent=`Interval ${index+1}`);});
  row.append(days,timeRow,modeLabel,remove);editor.append(row);return row;
}
function renderScheduleEditor(editor,intervals=[]){editor.replaceChildren();for(const interval of intervals)addScheduleInterval(editor,interval);}
function readScheduleEditor(editor){
  return [...editor.querySelectorAll(".schedule-interval")].map(row=>({
    days:[...row.querySelectorAll(".schedule-days input:checked")].map(input=>Number(input.value)),
    start:row.querySelector('[data-schedule-field="start"]').value,
    end:row.querySelector('[data-schedule-field="end"]').value,
    mode:row.querySelector('[data-schedule-field="mode"]').value,
  }));
}
function renderZoneEditor(container,zones=[]){
  const values=structuredClone(zones||[]);container.replaceChildren();container._zoneValues=values;
  const help=document.createElement("p");help.className="zone-help";help.textContent=values.length?"Drag on the frame to add a rectangle. Move or resize existing zones with the normalized coordinate fields.":"No zones use the full frame. Drag on the frame to add a zone, or add one with coordinates.";
  const stage=document.createElement("div");stage.className="zone-stage";stage.setAttribute("role","application");stage.setAttribute("aria-label","Normalized camera frame. Drag to draw a motion or AI zone.");
  const placeholder=document.createElement("span");placeholder.className="zone-stage-placeholder";placeholder.textContent="Representative frame loads when recent footage is available";stage.append(placeholder);
  for(const [index,zone] of values.entries()){
    const rect=document.createElement("div");rect.className="zone-rectangle";rect.setAttribute("aria-hidden","true");rect.style.left=`${zone.x*100}%`;rect.style.top=`${zone.y*100}%`;rect.style.width=`${zone.width*100}%`;rect.style.height=`${zone.height*100}%`;stage.append(rect);
  }
  let draw=null;
  const point=event=>{const box=stage.getBoundingClientRect();return {x:Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),y:Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))};};
  stage.addEventListener("pointerdown",event=>{if(event.target!==stage&&event.target!==placeholder)return;const start=point(event);draw={start,preview:document.createElement("div")};draw.preview.className="zone-rectangle zone-draft";stage.append(draw.preview);stage.setPointerCapture(event.pointerId);event.preventDefault();});
  stage.addEventListener("pointermove",event=>{if(!draw)return;const end=point(event),x=Math.min(draw.start.x,end.x),y=Math.min(draw.start.y,end.y),width=Math.abs(end.x-draw.start.x),height=Math.abs(end.y-draw.start.y);Object.assign(draw.preview.style,{left:`${x*100}%`,top:`${y*100}%`,width:`${width*100}%`,height:`${height*100}%`});});
  stage.addEventListener("pointerup",event=>{if(!draw)return;const end=point(event),x=Math.min(draw.start.x,end.x),y=Math.min(draw.start.y,end.y),width=Math.abs(end.x-draw.start.x),height=Math.abs(end.y-draw.start.y);draw.preview.remove();if(width>=.01&&height>=.01){markConfigDraftElement(container);values.push({x,y,width,height});renderZoneEditor(container,values);}draw=null;});
  const list=document.createElement("div");list.className="zone-list";
  values.forEach((zone,index)=>{
    const row=document.createElement("fieldset"),legend=document.createElement("legend");legend.textContent=`Zone ${index+1} coordinates`;row.append(legend);
    for(const field of ["x","y","width","height"]){const label=document.createElement("label");label.textContent=field.toUpperCase();const input=document.createElement("input");input.type="number";input.min=field==="width"||field==="height"?"0.001":"0";input.max="1";input.step="0.001";input.value=zone[field];input.dataset.zoneIndex=String(index);input.dataset.zoneField=field;input.addEventListener("input",()=>{const value=Number(input.value);if(Number.isFinite(value)){values[index][field]=value;const rect=stage.querySelectorAll(".zone-rectangle:not(.zone-draft)")[index];Object.assign(rect.style,{left:`${values[index].x*100}%`,top:`${values[index].y*100}%`,width:`${values[index].width*100}%`,height:`${values[index].height*100}%`});}});label.append(input);row.append(label);}
    const remove=document.createElement("button");remove.type="button";remove.textContent=`Remove zone ${index+1}`;remove.addEventListener("click",()=>{markConfigDraftElement(container);renderZoneEditor(container,values.filter((_,i)=>i!==index));});row.append(remove);list.append(row);
  });
  const add=document.createElement("button");add.type="button";add.textContent="Add zone by coordinates";add.addEventListener("click",()=>{markConfigDraftElement(container);values.push({x:.1,y:.1,width:.25,height:.25});renderZoneEditor(container,values);});
  container.append(help,stage,list,add);
}
function readZoneEditor(container){return (container._zoneValues||[]).map(zone=>({x:Number(zone.x),y:Number(zone.y),width:Number(zone.width),height:Number(zone.height)}));}
async function loadRepresentativeZoneFrame(cameraId,editor){
  try{
    const result=await api(`/api/cameras/${encodeURIComponent(cameraId)}/segments`),segment=result.segments?.at(-1);
    if(!segment)return;
    const at=segment.starts_at,url=`/api/cameras/${encodeURIComponent(cameraId)}/segments/${encodeURIComponent(segment.id)}/snapshot?at=${encodeURIComponent(at)}`;
    const image=new Image();image.alt="Representative recorded frame for zone placement";image.onload=()=>{const stage=editor.querySelector(".zone-stage");if(stage&&image.naturalWidth&&image.naturalHeight){stage.style.aspectRatio=`${image.naturalWidth} / ${image.naturalHeight}`;stage.setAttribute("aria-label",`Camera frame ${image.naturalWidth} by ${image.naturalHeight} pixels. Zones use normalized coordinates.`);stage.style.backgroundImage=`url("${url}")`;stage.classList.add("has-zone-frame");stage.querySelector(".zone-stage-placeholder")?.classList.add("hidden");}};image.src=url;
  }catch{}
}
async function requestPlaybackExport() {
  const message=document.querySelector("#export-message"),button=document.querySelector("#playback-export");
  try {
    const range=selectedExportRange();
    const camera_ids=[...new Set([...document.querySelectorAll("[data-export-camera]:checked")].map(input=>input.value))];
    const output_modes=[...document.querySelectorAll("[data-export-mode]:checked")].map(input=>input.dataset.exportMode);
    if(!camera_ids.length||camera_ids.length>16)throw new Error("Choose between one and sixteen searched cameras.");
    if(!output_modes.length)throw new Error("Choose individual camera videos, a synchronized grid, or both.");
    const eligible=playbackViews.filter(view=>camera_ids.includes(view.cameraId)&&view.segments.some(segment=>Math.min(range.end,playbackMilliseconds(segment.ends_at))>Math.max(range.start,playbackMilliseconds(segment.starts_at))));
    if(!eligible.length)throw new Error("No indexed footage overlaps this export selection.");
    const omitted=playbackViews.filter(view=>camera_ids.includes(view.cameraId)&&!eligible.includes(view)).map(view=>view.name);
    const jobCameraIds=output_modes.includes("grid")?camera_ids:eligible.map(view=>view.cameraId);
    const tileIds=playbackViews.filter(view=>jobCameraIds.includes(view.cameraId)).map(view=>view.cameraId);
    const requestedGridSize=document.querySelector("#playback-grid-size").value==="auto"?liveGridSizeFor(tileIds.length):Number(document.querySelector("#playback-grid-size").value)||4;
    const gridSize=[1,4,9,16].find(size=>size>=Math.max(requestedGridSize,tileIds.length))||16;
    const tile_camera_ids=[...tileIds,...Array(Math.max(0,gridSize-tileIds.length)).fill(null)];
    button.disabled=true;message.textContent="Submitting export…";
    const include_audio=document.querySelector("#export-include-audio").checked;
    const audio_camera_id=include_audio&&output_modes.includes("grid")?document.querySelector("#export-grid-audio-source").value:null;
    const job=await api("/api/exports",{method:"POST",body:JSON.stringify({camera_ids:jobCameraIds,tile_camera_ids,grid_resolution:document.querySelector("#export-grid-resolution").value,start:new Date(range.start).toISOString(),end:new Date(range.end).toISOString(),output_modes,include_audio,audio_camera_id})});
    const omissionNote=omitted.length?`; no footage for ${omitted.join(", ")}`:"";
    message.textContent=`Export ${job.id.slice(0,8)} queued (${output_modes.join(" + ")})${omissionNote}. You can leave this page; the job will continue.`;
    await refreshExports();
  }catch(error){message.textContent=error.message;}
  finally{updatePlaybackExportPreview();}
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
  if(!document.querySelector('#group-management-list [data-config-dirty="true"]'))renderGroupManagement(groups);
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
    const namePanel=document.createElement("fieldset"),nameLegend=document.createElement("legend");namePanel.dataset.configSection="group-name";nameLegend.textContent="Group name";namePanel.append(nameLegend);
    const nameLabel=document.createElement("label"),name=document.createElement("input");
    nameLabel.textContent="Group name ";name.value=group.name;name.maxLength=80;nameLabel.append(name);
    const rename=document.createElement("button");rename.type="button";rename.textContent="Save group name";
    rename.dataset.configSaves="group-name";
    rename.addEventListener("click",async()=>{
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id),{method:"PATCH",body:JSON.stringify({name:name.value})});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    const members=new Set(group.camera_ids||[]),fieldset=document.createElement("fieldset"),legend=document.createElement("legend");
    fieldset.dataset.configSection="group-members";legend.textContent="Browsing members";fieldset.append(legend);
    for(const camera of [...latestCameras].sort((a,b)=>a.name.localeCompare(b.name))){
      const label=document.createElement("label"),checkbox=document.createElement("input");
      checkbox.type="checkbox";checkbox.value=camera.id;checkbox.checked=members.has(camera.id);
      if(camera.group_id===group.id){checkbox.disabled=true;checkbox.title="This is the camera’s policy group and stays in its browsing group automatically.";}
      label.append(checkbox,document.createTextNode(" "+camera.name));fieldset.append(label);
    }
    const saveMembers=document.createElement("button");saveMembers.type="button";saveMembers.textContent="Save browsing members";
    saveMembers.dataset.configSaves="group-members";
    saveMembers.addEventListener("click",async()=>{
      const camera_ids=[...fieldset.querySelectorAll('input[type="checkbox"]:checked')].map(input=>input.value);
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id)+"/members",{method:"PUT",body:JSON.stringify({camera_ids})});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    const remove=document.createElement("button");remove.type="button";remove.textContent="Delete group";
    remove.addEventListener("click",async()=>{
      if(!window.confirm("Delete browsing group “"+group.name+"”? Groups that still own camera policies must be reassigned first."))return;
      if(!guardDraftsIn(card))return;
      try{await api("/api/camera-groups/"+encodeURIComponent(group.id),{method:"DELETE"});await refresh();}
      catch(error){document.querySelector("#schedule-message").textContent=error.message;}
    });
    namePanel.append(nameLabel,rename);card.append(heading,namePanel,fieldset,saveMembers,remove);return card;
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
function renderCameraInventoryTable(cameras) {
  const target=document.querySelector("#camera-inventory-table");
  if(!target)return;
  target.replaceChildren();
  if(!cameras.length){const empty=document.createElement("p");empty.textContent=latestCameras.length?"No cameras match these filters.":"No cameras have been added.";target.append(empty);return;}

  const table=document.createElement("table");table.className="camera-inventory-table";table.setAttribute("aria-label","Camera inventory");
  const head=document.createElement("thead"),headRow=document.createElement("tr");
    if(currentRole==="administrator"){const selectHeading=document.createElement("th");selectHeading.className="camera-select-column";selectHeading.scope="col";selectHeading.textContent="Select";headRow.append(selectHeading);}
  for(const [key,label] of [["name","Name"],["ip","IP address"],["group","Group"],["connection","Connection"],["health","Recording health"],["mode","Recording mode"]]){
    const cell=document.createElement("th");cell.scope="col";cell.setAttribute("aria-sort",cameraInventoryFilters.sortKey===key?(cameraInventoryFilters.sortDirection===1?"ascending":"descending"):"none");
    const sort=document.createElement("button");sort.type="button";sort.textContent=label;sort.dataset.sortKey=key;sort.addEventListener("click",()=>{
      if(cameraInventoryFilters.sortKey===key)cameraInventoryFilters.sortDirection*=-1;
      else{cameraInventoryFilters.sortKey=key;cameraInventoryFilters.sortDirection=1;}
      renderCameras(latestCameras);
    });cell.append(sort);headRow.append(cell);
  }
  head.append(headRow);table.append(head);
  const body=document.createElement("tbody");
  const selectCamera=async id=>{
    if(id===selectedCameraId){mobileCameraEditorOpen=true;renderCameras(latestCameras);renderSidebar();return;}
    if(id!==selectedCameraId&&!await guardCameraDrafts())return;
    selectedCameraId=id;cameraEditorState.selectCamera(id);clearCameraHistory();mobileCameraEditorOpen=true;
    renderCameras(latestCameras);renderSidebar();
  };
  for(const camera of cameras){
    const row=document.createElement("tr");row.dataset.cameraId=camera.id;row.classList.toggle("selected-camera",camera.id===selectedCameraId);row.tabIndex=0;
    if(currentRole==="administrator"){
      const selectCell=document.createElement("td"),checkbox=document.createElement("input");selectCell.className="camera-select-column";checkbox.type="checkbox";checkbox.dataset.cameraSelect="true";checkbox.value=camera.id;checkbox.checked=cameraBulkSelection.has(camera.id);checkbox.setAttribute("aria-label",`Select ${camera.name} for bulk changes`);checkbox.addEventListener("change",()=>{if(checkbox.checked)cameraBulkSelection.add(camera.id);else cameraBulkSelection.delete(camera.id);});selectCell.append(checkbox);row.append(selectCell);
    }
    const nameCell=document.createElement("td"),name=document.createElement("button");name.type="button";name.textContent=camera.archived_at?`${camera.name} · Archived`:camera.name;name.dataset.cameraId=camera.id;name.setAttribute("aria-pressed",String(camera.id===selectedCameraId));name.addEventListener("click",()=>void selectCamera(camera.id));nameCell.append(name);row.append(nameCell);
    const addCell=value=>{const cell=document.createElement("td");cell.textContent=value;row.append(cell);};
    addCell(camera.camera_ip||"Not reported");
    addCell(camera.group_name||"No group");
    addCell(window.NvrCameraInventory.stateLabel(camera.connection_status));
    addCell(window.NvrCameraInventory.stateLabel(camera.recording_health?.state));
    addCell(window.NvrCameraInventory.stateLabel(camera.effective_recording_mode||camera.recording_mode));
    row.addEventListener("click",event=>{if(event.target.closest("button,input,a"))return;void selectCamera(camera.id);});
    row.addEventListener("keydown",event=>{if(event.target===row&&(event.key==="Enter"||event.key===" ")){event.preventDefault();void selectCamera(camera.id);}});
    body.append(row);
  }
  table.append(body);target.append(table);
}
function clearCameraHistory(){
  const list=document.querySelector("#segment-list"),player=document.querySelector("#playback");
  list?.replaceChildren();
  if(player){player.pause();player.removeAttribute("src");player.load();player.classList.add("hidden");}
  const message=document.querySelector("#camera-message");if(message)message.textContent="";
}
function mobileCameraOpen(cameraId){
  selectedCameraId=cameraId;cameraEditorState.selectCamera(cameraId);mobileCameraEditorOpen=true;renderCameras(latestCameras);renderSidebar();
}
function renderCameras(cameras) {
  const list = document.querySelector("#camera-list");
  if(selectedCameraId&&!cameras.some(camera=>camera.id===selectedCameraId)){selectedCameraId=null;cameraEditorState.selectCamera(null);cameraPreviewWorkspace.clearSelection();mobileCameraEditorOpen=false;clearCameraHistory();}
  if(selectedCameraId&&cameraEditorState.selectedCameraId!==selectedCameraId)cameraEditorState.selectCamera(selectedCameraId);
  const filters={...cameraInventoryFilters,query:document.querySelector("#camera-inventory-search")?.value||""};
  const stateValue=value=>typeof value==="string"&&value.trim()?value.trim().toLocaleLowerCase():"unknown";
  for(const [id,values] of [
    ["camera-filter-group",[...(cameras.some(camera=>!camera.group_name)?["__none__"]:[]),...new Set(cameras.map(camera=>camera.group_name).filter(Boolean))].sort((a,b)=>a.localeCompare(b))],
    ["camera-filter-connection",[...new Set(cameras.map(camera=>stateValue(camera.connection_status)))].sort()],
    ["camera-filter-health",[...new Set(cameras.map(camera=>stateValue(camera.recording_health?.state)))].sort()],
  ]){
    const select=document.querySelector(`#${id}`);if(!select)continue;
    const prior=select.value;select.replaceChildren(new Option("All","all"),...values.map(value=>new Option(value==="__none__"?"No group":window.NvrCameraInventory.stateLabel(value),value)));
    select.value=[...select.options].some(option=>option.value===prior)?prior:"all";
    if(id==="camera-filter-group")cameraInventoryFilters.group=select.value;
    if(id==="camera-filter-connection")cameraInventoryFilters.connection=select.value;
    if(id==="camera-filter-health")cameraInventoryFilters.health=select.value;
  }
  Object.assign(filters,{group:cameraInventoryFilters.group,connection:cameraInventoryFilters.connection,health:cameraInventoryFilters.health,archive:cameraInventoryFilters.archive});
  const filtered=window.NvrCameraInventory.filterAndSortCameras(cameras,filters);
  renderCameraInventoryTable(filtered);
  const layout=document.querySelector("#camera-admin-layout");
  if(layout)layout.dataset.mobileEditor=String(mobileCameraEditorOpen&&Boolean(selectedCameraId));
  const selectedCamera=window.NvrCameraInventory.selectedCamera(cameras,selectedCameraId);
  const existingEditor=list.querySelector(':scope > article[data-camera-id]');
  const preserveDirtyEditor=Boolean(selectedCamera&&existingEditor?.dataset.cameraId===selectedCamera.id&&list.querySelector('[data-config-dirty="true"]'));
  if(!preserveDirtyEditor)list.replaceChildren();
  if(!cameras.length){cameraPreviewWorkspace.clearSelection();const empty=document.createElement("p");empty.className="camera-editor-empty";empty.textContent="No cameras have been added. Use Add Cameras to discover or add a camera.";list.append(empty);return;}
  if(!selectedCamera){cameraPreviewWorkspace.clearSelection();document.querySelector("#camera-health-history")?.classList.add("hidden");const empty=document.createElement("p");empty.className="camera-editor-empty";empty.textContent="Select a camera to view its settings and operational details.";list.append(empty);return;}
  const selectedIsFilteredOut=!filtered.some(camera=>camera.id===selectedCamera.id);
  if(selectedIsFilteredOut){
    let hiddenNotice=list.querySelector(".filtered-camera-notice");
    if(!hiddenNotice){hiddenNotice=document.createElement("p");hiddenNotice.className="filtered-camera-notice";hiddenNotice.setAttribute("role","status");hiddenNotice.append(document.createTextNode("The selected camera is hidden by the current table filters. Its settings remain open. "));
    const showInTable=document.createElement("button");showInTable.type="button";showInTable.textContent="Show in table";showInTable.addEventListener("click",()=>{
      const search=document.querySelector("#camera-inventory-search");if(search)search.value="";
      for(const id of ["camera-filter-archive","camera-filter-group","camera-filter-connection","camera-filter-health"]){const select=document.querySelector(`#${id}`);if(select)select.value="all";}
      Object.assign(cameraInventoryFilters,{archive:"all",group:"all",connection:"all",health:"all"});renderCameras(latestCameras);
    });
      hiddenNotice.append(showInTable);list.prepend(hiddenNotice);
    }
  }else list.querySelector(".filtered-camera-notice")?.remove();
  if(preserveDirtyEditor)return;
  list.append(...[selectedCamera].map(camera => {
    const card = document.createElement("article");
    card.classList.toggle("selected-camera",camera.id===selectedCameraId);
    const title = document.createElement("h3"); title.textContent = camera.name;
    if(camera.archived_at)title.textContent+=` · Archived ${formatDateTime(camera.archived_at)}`;
    const summary = document.createElement("p");
    const previewSlot=document.createElement("div");previewSlot.dataset.cameraPreviewSlot="";
    const video = camera.video || {};
    const effectiveMode=camera.effective_recording_mode || camera.recording_mode;
    const policyLabel=camera.recording_policy_source === "group" ? "group schedule" : camera.schedule_timezone ? "camera schedule" : "camera policy";
    const healthSummary=camera.recording_health||{};
    summary.textContent = (camera.group_name ? camera.group_name + " · " : "") + "storage " + (camera.storage_volume_id||"default") + " · " + (camera.recording_enabled ? "recording enabled" : "recording disabled") + " · effective " + effectiveMode + " (" + policyLabel + ") · " + camera.recording_status + " · coverage " + (healthSummary.state||"unknown") + (healthSummary.detail ? " — " + healthSummary.detail : "") + (healthSummary.last_video_at ? " · video seen " + healthSummary.last_video_at : "") + (healthSummary.last_footage_write_at ? " · footage written " + healthSummary.last_footage_write_at : "") + (camera.recording_mode === "motion" ? " · event source " + camera.motion_source_status + " · server fallback " + (camera.motion_fallback_enabled ? camera.server_motion_status : "disabled") : "") + " · audio " + (camera.audio_status||"unknown") + " · " + (video.codec || "unknown codec") + (video.width && video.height ? " " + video.width + "×" + video.height : "");
    const retentionSummary=camera.retention_coverage||{};
    summary.textContent += ` · retention ${camera.retention_days} days (${camera.retention_policy_source||"default"}; ${retentionSummary.achieved_days||0} days achieved` + (retentionSummary.projected_days_at_current_activity == null ? ")" : `; ${retentionSummary.projected_days_at_current_activity} days projected at current activity)`);
    card.append(title, summary, previewSlot);
    card.dataset.cameraId=camera.id;
    if(currentRole === "administrator"&&camera.id===selectedCameraId) {
      const editor=document.createElement("details"),editorHeading=document.createElement("summary");
      editor.dataset.configSection="camera-details";
      editor.dataset.cameraTab="general-connection";
      editorHeading.textContent="Edit camera details";
      const nameLabel=document.createElement("label"),nameInput=document.createElement("input");
      nameLabel.textContent="Camera name ";nameInput.type="text";nameInput.maxLength=120;nameInput.required=true;nameInput.value=camera.name;nameLabel.append(nameInput);
      const saveDetails=document.createElement("button");saveDetails.type="button";saveDetails.textContent="Save camera details";
      saveDetails.dataset.configSaves="camera-details";
      editor.cameraSaveConfig=async()=>{await api(`/api/cameras/${encodeURIComponent(camera.id)}`,{method:"PATCH",body:JSON.stringify({name:nameInput.value})});};
      saveDetails.addEventListener("click",()=>void saveCameraScopes([editor],{refreshAfter:true}));
      editor.append(editorHeading,nameLabel,saveDetails);card.append(editor);
      const sourcePanel=document.createElement("fieldset"),sourceLegend=document.createElement("legend");sourcePanel.dataset.configSection="camera-source";sourceLegend.textContent="Video source and advertised ONVIF profiles";sourcePanel.append(sourceLegend);
      const addressLabel=document.createElement("label"),address=document.createElement("input");addressLabel.textContent="Camera IP address ";address.inputMode="numeric";address.autocomplete="off";address.value=camera.camera_ip||"";addressLabel.append(address);
      const directLabel=document.createElement("label"),directUrl=document.createElement("input");directLabel.textContent="Direct RTSP URL (use when not selecting by IP) ";directUrl.type="url";directUrl.autocomplete="off";directUrl.placeholder="rtsp://camera.example/stream";directLabel.append(directUrl);
      const usernameLabel=document.createElement("label"),username=document.createElement("input");usernameLabel.textContent="Camera username (leave blank to keep current credentials) ";username.autocomplete="off";usernameLabel.append(username);
      const passwordLabel=document.createElement("label"),password=document.createElement("input");passwordLabel.textContent="Camera password (leave blank to keep current credentials) ";password.type="password";password.autocomplete="new-password";passwordLabel.append(password);
      const confirmLabel=document.createElement("label"),confirm=document.createElement("input");confirmLabel.textContent="Confirm new camera password ";confirm.type="password";confirm.autocomplete="new-password";confirmLabel.append(confirm);
      const clearLabel=document.createElement("label"),clearCredentials=document.createElement("input");clearCredentials.type="checkbox";clearLabel.append(clearCredentials,document.createTextNode(" Clear saved camera credentials"));
      const brandLabel=document.createElement("label"),brand=document.createElement("select");brandLabel.textContent="Device type / brand ";
      for(const [value,label] of [["auto","Auto-detect / ONVIF"],["acti","ACTi"],["axis","Axis"],["bosch","Bosch"],["dahua","Dahua"],["hanwha","Hanwha"],["hikvision","Hikvision"],["reolink","Reolink"],["samsung","Samsung"],["uniview","Uniview"],["other","Other / generic ONVIF"]])brand.add(new Option(label,value));
      brand.value=camera.camera_brand||"auto";brandLabel.append(brand);
      const serviceLabel=document.createElement("label"),service=document.createElement("input");serviceLabel.textContent="ONVIF device-service URL (optional custom path) ";service.type="url";service.autocomplete="off";service.value=camera.device_service_url||"";service.placeholder="http://camera/onvif/device_service";serviceLabel.append(service);
      const mainLabel=document.createElement("label"),mainProfile=document.createElement("select");mainLabel.textContent="Main stream profile ";mainProfile.append(new Option(camera.onvif_profile_token?`Saved profile · ${camera.onvif_profile_token}`:"Load advertised profiles",camera.onvif_profile_token||""));mainLabel.append(mainProfile);
      const subLabel=document.createElement("label"),subProfile=document.createElement("select");subLabel.textContent="Optional lower-resolution live grid profile ";subProfile.append(new Option("Use main stream for grid scaled by server",""));if(camera.live_substream_profile_token)subProfile.add(new Option(`Saved profile · ${camera.live_substream_profile_token}`,camera.live_substream_profile_token));subLabel.append(subProfile);
      const sourceMessage=document.createElement("p");sourceMessage.setAttribute("role","status");
      const loadProfiles=document.createElement("button");loadProfiles.type="button";loadProfiles.textContent="Load advertised endpoints";
      const loadCameraProfilesForEditor=async()=>{
        try{
          if(password.value!==confirm.value)throw new Error("Camera passwords do not match");
          const selection=cameraEditorState.captureSelection();
          const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/source/profiles`,{method:"POST",body:JSON.stringify({camera_ip:address.value.trim(),camera_username:username.value||null,camera_password:password.value||null,camera_brand:brand.value,device_service_url:service.value||null,clear_credentials:clearCredentials.checked})});
          if(!cameraEditorState.isCurrent(selection))return;
          mainProfile.replaceChildren(...result.profiles.map(profile=>new Option(profile.name,profile.token)));
          if(result.profiles.length>1)mainProfile.prepend(new Option("Select main stream profile",""));
          subProfile.replaceChildren(new Option("Use main stream for grid scaled by server",""),...result.profiles.map(profile=>new Option(profile.name,profile.token)));
          mainProfile.value=camera.onvif_profile_token||result.profiles[0]?.token||"";subProfile.value=camera.live_substream_profile_token||"";service.value=result.device_service_url;
          sourceMessage.textContent=`Found ${result.profiles.length} advertised ONVIF profile(s).`;
        }catch(error){sourceMessage.textContent=error.message;}
      };
      sourcePanel.cameraLoadProfiles=loadCameraProfilesForEditor;
      loadProfiles.addEventListener("click",async()=>{
        if(sourcePanel.dataset.configDirty==="true"){
          if(!await guardCameraDrafts())return;
          const current=document.querySelector(`#camera-list [data-config-section="camera-source"]`);
          await current?.cameraLoadProfiles?.();
        }else await loadCameraProfilesForEditor();
      });
      const buildSourcePayload=()=>{
        if(password.value!==confirm.value)throw new Error("Camera passwords do not match");
        const common={camera_brand:brand.value,device_service_url:service.value||null,clear_credentials:clearCredentials.checked};
        if(address.value.trim()){
          if(!mainProfile.value)throw new Error("Load advertised endpoints and select a main stream profile first");
          return {...common,camera_ip:address.value.trim(),camera_username:username.value||null,camera_password:password.value||null,onvif_profile_token:mainProfile.value,live_substream_profile_token:subProfile.value||null};
        }
        if(!directUrl.value)throw new Error("Enter a camera IP address or a direct RTSP URL");
        return {...common,stream_url:directUrl.value.trim()};
      };
      const testSource=document.createElement("button");testSource.type="button";testSource.textContent="Test source connection";
      testSource.addEventListener("click",async()=>{
        try{const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/source/test`,{method:"POST",body:JSON.stringify(buildSourcePayload())});sourceMessage.textContent=result.reachable?`Connection passed · ${result.video.codec} ${result.video.width||"?"}×${result.video.height||"?"}`:result.reason;}
        catch(error){sourceMessage.textContent=error.message;}
      });
      const saveSource=document.createElement("button");saveSource.type="button";saveSource.textContent="Save tested source";
      saveSource.dataset.configSaves="camera-source";
      sourcePanel.cameraSaveConfig=async()=>{await api(`/api/cameras/${encodeURIComponent(camera.id)}/source`,{method:"PUT",body:JSON.stringify(buildSourcePayload())});};
      saveSource.addEventListener("click",()=>void saveCameraScopes([sourcePanel],{refreshAfter:true}));
      sourcePanel.append(addressLabel,directLabel,usernameLabel,passwordLabel,confirmLabel,clearLabel,brandLabel,serviceLabel,loadProfiles,mainLabel,subLabel,testSource,saveSource,sourceMessage);editor.append(sourcePanel);
      const audioPanel=document.createElement("fieldset"),audioLegend=document.createElement("legend");audioPanel.dataset.configSection="camera-audio";audioLegend.textContent="Audio";audioPanel.append(audioLegend);
      const audioLabel=document.createElement("label");audioLabel.textContent="Enable audio recording and operator listening ";
      const audioEnabled=document.createElement("input");audioEnabled.type="checkbox";audioEnabled.checked=Boolean(camera.audio_enabled);audioEnabled.disabled=camera.audio_supported!==true;
      audioLabel.append(audioEnabled);
      const saveAudio=document.createElement("button");saveAudio.type="button";saveAudio.textContent="Apply audio setting";
      saveAudio.dataset.configSaves="camera-audio";
      audioPanel.cameraSaveConfig=async()=>{await api(`/api/cameras/${encodeURIComponent(camera.id)}`,{method:"PATCH",body:JSON.stringify({audio_enabled:audioEnabled.checked})});};
      saveAudio.addEventListener("click",()=>void saveCameraScopes([audioPanel],{refreshAfter:true}));
      audioPanel.append(audioLabel,saveAudio);card.append(audioPanel);
      if(camera.audio_supported==null) {
        const probeAudio=document.createElement("button");probeAudio.type="button";probeAudio.textContent="Probe camera audio";
        probeAudio.dataset.cameraTab="general-connection";
        probeAudio.addEventListener("click",async()=>{const selection=cameraEditorState.captureSelection();try{const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/audio/probe`,{method:"POST"});if(!cameraEditorState.isCurrent(selection))return;document.querySelector("#camera-message").textContent=result.detail;await refresh();}catch(error){if(cameraEditorState.isCurrent(selection))document.querySelector("#camera-message").textContent=error.message;}});
        card.append(probeAudio);
      }
    }
    if(currentRole === "administrator") {
      const retentionPanel=document.createElement("fieldset"),retentionLegend=document.createElement("legend");retentionPanel.dataset.configSection="camera-retention";retentionLegend.textContent="Retention";retentionPanel.append(retentionLegend);
      const retentionLabel=document.createElement("label"); retentionLabel.textContent="Camera retention override (days)";
      const retentionDays=document.createElement("input"); retentionDays.type="number"; retentionDays.min="1"; retentionDays.max="3650"; retentionDays.value=String(camera.retention_days||30);
      const inheritRetentionLabel=document.createElement("label");inheritRetentionLabel.textContent="Inherit group or default retention ";const inheritRetentionValue=document.createElement("input");inheritRetentionValue.type="checkbox";inheritRetentionValue.checked=camera.retention_policy_source!=="camera";inheritRetentionLabel.append(inheritRetentionValue);
      const saveRetention=document.createElement("button"); saveRetention.type="button"; saveRetention.textContent="Save camera retention";
      saveRetention.dataset.configSaves="camera-retention";
      const inheritRetention=document.createElement("button"); inheritRetention.type="button"; inheritRetention.textContent="Use inherited retention";
      inheritRetention.addEventListener("click",()=>{inheritRetentionValue.checked=true;markConfigDraftElement(retentionPanel);});
      retentionPanel.cameraApplyConfig=policy=>{retentionDays.value=String(policy.retention_days||30);inheritRetentionValue.checked=Boolean(policy.inherits_group);};
      retentionPanel.cameraSaveConfig=async()=>{if(inheritRetentionValue.checked)await api(`/api/cameras/${encodeURIComponent(camera.id)}/retention`,{method:"DELETE"});else await api(`/api/cameras/${encodeURIComponent(camera.id)}/retention`,{method:"PUT",body:JSON.stringify({retention_days:Number(retentionDays.value)})});};
      saveRetention.addEventListener("click",()=>void saveCameraScopes([retentionPanel],{refreshAfter:true}));
      retentionPanel.append(retentionLabel,retentionDays,inheritRetentionLabel,saveRetention,inheritRetention);card.append(retentionPanel);
    }
    if (camera.last_error) { const note=document.createElement("p"); note.textContent=camera.last_error;note.dataset.cameraTab="health-history"; card.append(note); }
    const button = document.createElement("button"); button.type="button"; button.textContent="Find recorded footage";
    button.dataset.cameraTab="health-history";
    button.addEventListener("click", () => loadSegments(camera)); card.append(button);
    if (currentRole !== "administrator") {document.querySelector("#camera-health-history")?.classList.remove("hidden");cameraPreviewWorkspace.mount(previewSlot,camera);return card;}
    const mode=document.createElement("select"); mode.setAttribute("aria-label",`Recording mode for ${camera.name}`);
    const recordingPanel=document.createElement("fieldset"),recordingLegend=document.createElement("legend");recordingPanel.dataset.configSection="camera-recording-policy";recordingLegend.textContent="Recording policy";recordingPanel.append(recordingLegend);
    const motionRollPanel=document.createElement("fieldset"),motionRollLegend=document.createElement("legend");motionRollPanel.dataset.configSection="camera-motion-roll";motionRollPanel.dataset.cameraSaveGroup="camera-motion";motionRollLegend.textContent="Motion clip timing";motionRollPanel.append(motionRollLegend);
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
    save.dataset.configSaves="camera-recording-policy";
    recordingPanel.cameraSaveConfig=async()=>{await api(`/api/cameras/${encodeURIComponent(camera.id)}`, { method:"PATCH", body:JSON.stringify({recording_mode:mode.value,recording_enabled:enabled.checked}) });};
    save.addEventListener("click",()=>void saveCameraScopes([recordingPanel],{refreshAfter:true}));
    const motionUrlLabel=document.createElement("label"); motionUrlLabel.textContent="ONVIF device service URL for motion events";
    const motionUrl=document.createElement("input"); motionUrl.type="url"; motionUrl.autocomplete="off"; motionUrl.value=camera.device_service_url||"";motionUrl.placeholder="http://camera/onvif/device_service";
    const motionUserLabel=document.createElement("label"); motionUserLabel.textContent="ONVIF username";
    const motionUser=document.createElement("input"); motionUser.autocomplete="off";
    const motionPassLabel=document.createElement("label"); motionPassLabel.textContent="ONVIF password";
    const motionPass=document.createElement("input"); motionPass.type="password"; motionPass.autocomplete="new-password";
    const fallbackLabel=document.createElement("label"); fallbackLabel.textContent="Use server motion detection when ONVIF events are unavailable ";
    const fallback=document.createElement("input"); fallback.type="checkbox"; fallback.checked=camera.motion_fallback_enabled;
    fallbackLabel.append(fallback);
    const zonesLabel=document.createElement("label"); zonesLabel.textContent="Server motion zones (normalized rectangles; empty uses full frame)";
    const zones=document.createElement("textarea"); zones.spellcheck=false; zones.value=JSON.stringify(camera.motion_zones||[]); zones.placeholder='[{"x":0.1,"y":0.1,"width":0.8,"height":0.8}]';
    const zoneEditor=document.createElement("div");zoneEditor.setAttribute("aria-label","Visual server motion zones");renderZoneEditor(zoneEditor,camera.motion_zones||[]);loadRepresentativeZoneFrame(camera.id,zoneEditor);
    const zonesAdvanced=document.createElement("details"),zonesSummary=document.createElement("summary");zonesSummary.textContent="Advanced motion zone JSON";zonesAdvanced.append(zonesSummary,zonesLabel,zones);
    const sensitivityLabel=document.createElement("label"); sensitivityLabel.textContent="Server motion sensitivity (1–100)";
    const sensitivity=document.createElement("input"); sensitivity.type="number"; sensitivity.min="1"; sensitivity.max="100"; sensitivity.value=camera.motion_sensitivity||50;
    const configureMotion=document.createElement("button"); configureMotion.type="button"; configureMotion.textContent="Save motion event source";
    const motionSourcePanel=document.createElement("fieldset"),motionSourceLegend=document.createElement("legend");motionSourcePanel.dataset.configSection="camera-motion-source";motionSourceLegend.textContent="Motion event source and detection";motionSourcePanel.append(motionSourceLegend);
    motionSourcePanel.dataset.cameraSaveGroup="camera-motion";
    configureMotion.dataset.configSaves="camera-motion-roll camera-motion-source";
    const saveMotionSource=async()=>{
      const payload={pre_roll_seconds:Number(pre.value),post_roll_seconds:Number(post.value),fallback_enabled:fallback.checked,zones:readZoneEditor(zoneEditor),sensitivity:Number(sensitivity.value)};
      if(motionUrl.value) payload.service_url=motionUrl.value;
      if(motionUser.value||motionPass.value){payload.username=motionUser.value;payload.password=motionPass.value;}
      await api(`/api/cameras/${encodeURIComponent(camera.id)}/motion-source`,{method:"POST",body:JSON.stringify(payload)});
      motionUser.value=""; motionPass.value="";
    };
    motionRollPanel.cameraSaveConfig=saveMotionSource;motionSourcePanel.cameraSaveConfig=saveMotionSource;
    configureMotion.addEventListener("click",()=>void saveCameraScopes([motionSourcePanel],{refreshAfter:true}));
    const events=document.createElement("button"); events.type="button"; events.textContent="View motion events";events.dataset.cameraTab="motion";
    const motionEventsResults=document.createElement("div");motionEventsResults.dataset.cameraTab="motion";motionEventsResults.setAttribute("aria-live","polite");
    events.addEventListener("click",async()=>{
      const selection=cameraEditorState.captureSelection();
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/motion-events`);
        if(!cameraEditorState.isCurrent(selection))return;
        const target=motionEventsResults;
        target.replaceChildren(...result.events.map(event=>{
          const row=document.createElement("p");
          row.textContent=formatDateTime(event.source_event_at)+" · "+event.source+" · "+event.status+" · pre-roll "+(event.pre_roll_complete ? "complete" : "incomplete/unknown")+(event.media_url ? " · playable clip available" : "");
          if(event.media_url) { const play=document.createElement("button"); play.type="button"; play.textContent="Play"; play.addEventListener("click",async()=>{if(!await navigateCameraTab("health-history"))return;const player=document.querySelector("#playback");player.src=event.media_url;player.classList.remove("hidden");player.play().catch(()=>{});}); row.append(play); }
          return row;
        }));
      } catch(error) { if(cameraEditorState.isCurrent(selection))document.querySelector("#camera-message").textContent=error.message; }
    });
    const aiEvents=document.createElement("button"); aiEvents.type="button"; aiEvents.textContent="Search person/vehicle events";aiEvents.dataset.cameraTab="ai";
    const aiEventResults=document.createElement("div");aiEventResults.dataset.cameraTab="ai";aiEventResults.setAttribute("aria-live","polite");
    aiEvents.addEventListener("click",async()=>{
      try {
        const label=document.createElement("select");
        for(const [value,text] of [["","All detections"],["person","Person"],["vehicle","Vehicle"]]){const option=document.createElement("option");option.value=value;option.textContent=text;label.append(option);}
        const from=document.createElement("input");from.type="datetime-local";from.setAttribute("aria-label","AI event start time");
        const to=document.createElement("input");to.type="datetime-local";to.setAttribute("aria-label","AI event end time");
        const run=document.createElement("button");run.type="button";run.textContent="Apply AI event search";
        const results=document.createElement("div");results.setAttribute("aria-live","polite");
        const selection=cameraEditorState.captureSelection();
        const search=async()=>{
          const params=new URLSearchParams({camera_id:camera.id});if(label.value)params.set("label",label.value);
          if(from.value)params.set("start",new Date(from.value).toISOString());if(to.value)params.set("end",new Date(to.value).toISOString());
          const found=await api(`/api/ai/events?${params}`);
          if(!cameraEditorState.isCurrent(selection))return;
          results.replaceChildren();
          const note=document.createElement("p");note.textContent=found.coverage_note;results.append(note);
          for(const item of found.events){const row=document.createElement("p");row.textContent=`${formatDateTime(item.first_seen_at)} · ${item.label} · ${(item.confidence*100).toFixed(1)}% · ${item.model_id} ${item.model_version} · OpenVINO ${item.provider}`;
            const play=document.createElement("button");play.type="button";play.textContent="Find playback";play.addEventListener("click",async()=>{
              const at=new Date(item.first_seen_at),begin=new Date(at.getTime()-30000),finish=new Date(at.getTime()+30000);
              const footage=await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments?from=${encodeURIComponent(begin.toISOString())}&to=${encodeURIComponent(finish.toISOString())}`);
              if(!cameraEditorState.isCurrent(selection))return;
              const segment=footage.segments.find(s=>new Date(s.starts_at)<=at&&new Date(s.ends_at)>=at)||footage.segments[0];
              if(!cameraEditorState.isCurrent(selection))return;
              if(!segment){row.append(document.createTextNode(" · no retained footage covers this observation"));return;}
              if(!await navigateCameraTab("health-history"))return;const player=document.querySelector("#playback");player.src=segment.media_url;player.classList.remove("hidden");
              player.addEventListener("loadedmetadata",()=>{const offset=Math.max(0,(at-new Date(segment.starts_at))/1000);player.currentTime=offset;},{once:true});player.play().catch(()=>{});
            });row.append(play);results.append(row);
          }
          if(!found.events.length){const empty=document.createElement("p");empty.textContent="No analyzed detections match this search. Check analysis coverage; empty results do not prove the scene was empty.";results.append(empty);}
        };
        run.addEventListener("click",async()=>{try{await search();}catch(error){results.textContent=error.message;}});
        await search();
        if(!cameraEditorState.isCurrent(selection))return;
        aiEventResults.replaceChildren(label,from,to,run,results);
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const aiPanel=document.createElement("section");aiPanel.dataset.configSection="camera-ai";
    const aiConfigure=document.createElement("button");aiConfigure.type="button";aiConfigure.textContent="Configure person/vehicle detection";aiConfigure.dataset.cameraTab="ai";
    const loadAiConfig=async(configOverride=null)=>{
        if(aiPanel.dataset.configDirty==="true"&&!configOverride)throw new Error("Save or revert AI settings before reloading them.");
        const selection=cameraEditorState.captureSelection();
        const config=configOverride||await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai`);
        if(!cameraEditorState.isCurrent(selection))return;
        aiPanel.replaceChildren();const heading=document.createElement("h4");heading.textContent="Local CPU AI analysis";
        const availability=document.createElement("p");availability.textContent=`${config.model.id} ${config.model.version} · ${config.model.runtime} · ${config.model.available?"model ready":config.model.reason}`;
        const source=document.createElement("p");source.textContent=`Effective settings source: ${config.config_source}${config.config_source==="group"?" (inherited)":""}`;
        const inheritLabel=document.createElement("label");inheritLabel.textContent="Inherit assigned camera group AI settings ";const inherit=document.createElement("input");inherit.type="checkbox";inherit.dataset.aiField="inherit";inherit.checked=!config.config_override&&config.config_source==="group";inheritLabel.append(inherit);
        const enabledLabel=document.createElement("label");enabledLabel.textContent="Enable analysis ";const aiEnabled=document.createElement("input");aiEnabled.type="checkbox";aiEnabled.dataset.aiField="enabled";aiEnabled.checked=config.enabled;enabledLabel.append(aiEnabled);
        const fpsLabel=document.createElement("label");fpsLabel.textContent="Analysis frames per second";const fps=document.createElement("input");fps.type="number";fps.min="0.1";fps.max="5";fps.step="0.1";fps.dataset.aiField="fps";fps.value=config.analysis_fps;fpsLabel.append(fps);
        const confidenceLabel=document.createElement("label");confidenceLabel.textContent="Minimum confidence (0.1–0.99)";const confidence=document.createElement("input");confidence.type="number";confidence.min="0.1";confidence.max="0.99";confidence.step="0.01";confidence.dataset.aiField="confidence";confidence.value=config.confidence_threshold;confidenceLabel.append(confidence);
        const substreamLabel=document.createElement("label");substreamLabel.textContent="Use validated lower-resolution stream ";const substream=document.createElement("input");substream.type="checkbox";substream.dataset.aiField="substream";substream.checked=config.use_substream;substreamLabel.append(substream);
        const zonesLabel=document.createElement("label");zonesLabel.textContent="AI zones (normalized rectangles; empty uses full frame)";const aiZones=document.createElement("textarea");aiZones.spellcheck=false;aiZones.value=JSON.stringify(config.zones);aiZones.placeholder='[{"x":0.1,"y":0.1,"width":0.8,"height":0.8}]';zonesLabel.append(aiZones);
        const aiZoneEditor=document.createElement("div");aiZoneEditor.dataset.aiField="zones";aiZoneEditor.setAttribute("aria-label","Visual AI zones");renderZoneEditor(aiZoneEditor,config.zones||[]);loadRepresentativeZoneFrame(camera.id,aiZoneEditor);
        const aiZonesAdvanced=document.createElement("details"),aiZonesSummary=document.createElement("summary");aiZonesSummary.textContent="Advanced AI zone JSON";aiZonesAdvanced.append(aiZonesSummary,zonesLabel);
        const saveAI=document.createElement("button");saveAI.type="button";saveAI.textContent="Save AI settings";
        saveAI.dataset.configSaves="camera-ai";
        const aiStatus=document.createElement("p");aiStatus.textContent=`Status: ${config.status}${config.last_success_at?` · last analyzed ${formatDateTime(config.last_success_at)}`:""}${config.last_error?` · ${config.last_error}`:""}`;
        const saveAiSettings=async()=>{const selectedZones=readZoneEditor(aiZoneEditor);aiZones.value=JSON.stringify(selectedZones);const saved=await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai`,{method:"PUT",body:JSON.stringify({enabled:aiEnabled.checked,analysis_fps:Number(fps.value),confidence_threshold:Number(confidence.value),use_substream:substream.checked,zones:selectedZones,inherit_group:inherit.checked})});aiStatus.textContent=`Status: ${saved.status} · settings ${saved.config_source}${saved.last_error?` · ${saved.last_error}`:""}`;};
        aiPanel.cameraSaveConfig=saveAiSettings;
        saveAI.addEventListener("click",()=>void saveCameraScopes([aiPanel],{refreshAfter:false,statusTabId:"ai"}));
        const coverageButton=document.createElement("button");coverageButton.type="button";coverageButton.textContent="Refresh analysis coverage";coverageButton.addEventListener("click",async()=>{try{const selection=cameraEditorState.captureSelection();const status=await api(`/api/cameras/${encodeURIComponent(camera.id)}/ai/coverage`);if(!cameraEditorState.isCurrent(selection))return;aiStatus.textContent=`Status: ${status.status} · ${status.intervals.length} analysis coverage intervals${status.last_error?` · ${status.last_error}`:""}`;}catch(error){aiStatus.textContent=error.message;}});
        aiPanel.append(heading,availability,source,inheritLabel,enabledLabel,fpsLabel,confidenceLabel,substreamLabel,aiZoneEditor,aiZonesAdvanced,saveAI,coverageButton,aiStatus);
    };
    aiPanel.cameraLoadConfig=loadAiConfig;
    aiConfigure.addEventListener("click",async()=>{try{
      if(aiPanel.dataset.configDirty==="true"){
        if(!await guardCameraDrafts())return;
        const current=document.querySelector(`#camera-list [data-config-section="camera-ai"]`);
        await current?.cameraLoadConfig?.();
      }else await loadAiConfig();
    }catch(error){document.querySelector("#camera-message").textContent=error.message;}});
    const coverage=document.createElement("button"); coverage.type="button"; coverage.textContent="View coverage history";coverage.dataset.cameraTab="health-history";
    const coverageList=document.createElement("ul");
    coverage.addEventListener("click",async()=>{
      const selection=cameraEditorState.captureSelection();
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/coverage`);
        if(!cameraEditorState.isCurrent(selection))return;
        coverageList.replaceChildren(...result.intervals.map(interval=>{
          const item=document.createElement("li");
          const end=interval.ends_at ? formatDateTime(interval.ends_at) : "ongoing";
          item.textContent=`${interval.coverage_kind}: ${interval.reason} · ${formatDateTime(interval.starts_at)} to ${end}`;
          return item;
        }));
        if(!result.intervals.length){const item=document.createElement("li");item.textContent="No coverage transitions recorded yet.";coverageList.replaceChildren(item);}
      } catch(error) { if(cameraEditorState.isCurrent(selection))document.querySelector("#camera-message").textContent=error.message; }
    });
    const scheduleLabel=document.createElement("label"); scheduleLabel.textContent="Camera weekly recording schedule override (JSON; blank intervals mean scheduled idle)";
    const schedulePanel=document.createElement("fieldset"),scheduleLegend=document.createElement("legend");schedulePanel.dataset.configSection="camera-schedule";scheduleLegend.textContent="Recording schedule";schedulePanel.append(scheduleLegend);
    const scheduleText=document.createElement("textarea"); scheduleText.spellcheck=false; scheduleText.placeholder='{"timezone":"America/Indiana/Indianapolis","intervals":[{"days":[0,1,2,3,4],"start":"08:00","end":"17:00","mode":"continuous"}]}';
    const scheduleTimezoneLabel=document.createElement("label");scheduleTimezoneLabel.textContent="Schedule time zone (IANA)";const scheduleTimezone=document.createElement("input");scheduleTimezone.maxLength=128;scheduleTimezone.value=camera.schedule_timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC";scheduleTimezoneLabel.append(scheduleTimezone);
    const scheduleEditor=document.createElement("div");scheduleEditor.setAttribute("aria-label","Visual camera weekly schedule");
    const addSchedule=document.createElement("button");addSchedule.type="button";addSchedule.textContent="Add schedule interval";addSchedule.addEventListener("click",()=>{markConfigDraftElement(scheduleEditor);addScheduleInterval(scheduleEditor);});
    const scheduleAdvanced=document.createElement("details"),scheduleSummary=document.createElement("summary");scheduleSummary.textContent="Advanced schedule JSON";scheduleAdvanced.append(scheduleSummary,scheduleLabel,scheduleText);
    const inheritLabel=document.createElement("label"); inheritLabel.textContent="Inherit assigned group schedule ";
    const inheritSchedule=document.createElement("input"); inheritSchedule.type="checkbox"; inheritSchedule.checked=true; inheritLabel.append(inheritSchedule);
    const loadSchedule=document.createElement("button"); loadSchedule.type="button"; loadSchedule.textContent="Load camera schedule";
    const applyCameraSchedule=result=>{
      inheritSchedule.checked=result.inherits_group;
      scheduleTimezone.value=result.timezone||"UTC";
      renderScheduleEditor(scheduleEditor,result.intervals||[]);
      scheduleText.value=JSON.stringify({timezone:result.timezone,intervals:result.intervals},null,2);
      document.querySelector("#camera-message").textContent=`Loaded ${result.source} schedule${result.configured ? "" : " (no schedule configured)"}.`;
    };
    const loadCameraSchedule=async()=>{
      const selection=cameraEditorState.captureSelection();
      const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/schedule`);
      if(!cameraEditorState.isCurrent(selection))return;
      applyCameraSchedule(result);
    };
    schedulePanel.cameraLoadConfig=loadCameraSchedule;
    schedulePanel.cameraApplyConfig=applyCameraSchedule;
    loadSchedule.addEventListener("click",async()=>{
      try{
        if(schedulePanel.dataset.configDirty==="true"){
          if(!await guardCameraDrafts())return;
          const current=document.querySelector(`#camera-list [data-config-section="camera-schedule"]`);
          await current?.cameraLoadConfig?.();
        }else await loadCameraSchedule();
      }catch(error){document.querySelector("#camera-message").textContent=error.message;}
    });
    const saveSchedule=document.createElement("button"); saveSchedule.type="button"; saveSchedule.textContent="Save camera schedule";
    saveSchedule.dataset.configSaves="camera-schedule";
    schedulePanel.cameraSaveConfig=async()=>{
      const config=inheritSchedule.checked ? {} : {timezone:scheduleTimezone.value.trim()||"UTC",intervals:readScheduleEditor(scheduleEditor)};
      const payload=inheritSchedule.checked ? {inherit_group:true} : config;
      await api(`/api/cameras/${encodeURIComponent(camera.id)}/schedule`,{method:"PUT",body:JSON.stringify(payload)});
      scheduleText.value=JSON.stringify({timezone:payload.timezone,intervals:payload.intervals},null,2);
    };
    saveSchedule.addEventListener("click",()=>void saveCameraScopes([schedulePanel],{refreshAfter:true}));
    const archive=document.createElement("button"); archive.type="button"; archive.textContent=camera.archived_at?"Restore camera":"Archive camera";archive.dataset.cameraTab="health-history";
    archive.addEventListener("click", async () => {
      const tiles=[...activeLiveTileCameraIds],ids=[...activeLiveCameraIds],size=activeLiveGridSize,focused=focusedLiveCameraId;
      if(!camera.archived_at&&!window.confirm(`Archive ${camera.name}? Collection stops, and its recordings remain available for search and export.`))return;
      try {
        if(camera.archived_at)await api(`/api/cameras/${encodeURIComponent(camera.id)}/restore`,{method:"POST"});
        else await api(`/api/cameras/${encodeURIComponent(camera.id)}`,{method:"DELETE"});
        if(!camera.archived_at)cameraPreviewWorkspace.close();
        await refresh();
        if(ids.includes(camera.id))startLiveViews(ids,latestCameras,focused,size,tiles);
      } catch (error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const deleteFootage=document.createElement("button");deleteFootage.type="button";deleteFootage.textContent="Permanently delete archived footage";deleteFootage.dataset.cameraTab="health-history";deleteFootage.disabled=!camera.archived_at;
    deleteFootage.addEventListener("click",async()=>{
      const confirmation=window.prompt(`This permanently deletes every recording for ${camera.name}. Type the camera name to confirm.`);
      if(confirmation!==camera.name)return;
      try{await api(`/api/cameras/${encodeURIComponent(camera.id)}/footage`,{method:"DELETE",body:JSON.stringify({confirm_camera_name:confirmation})});document.querySelector("#camera-message").textContent=`Archived footage for ${camera.name} deleted.`;await refresh();}
      catch(error){document.querySelector("#camera-message").textContent=error.message;}
    });
    const rotateLabel=document.createElement("label"); rotateLabel.textContent="Update stream credentials or URL";
    const rotatePanel=document.createElement("fieldset"),rotateLegend=document.createElement("legend");rotatePanel.dataset.configSection="camera-credentials";rotateLegend.textContent="Stream credentials";rotatePanel.append(rotateLegend);
    const rotateInput=document.createElement("input"); rotateInput.type="url"; rotateInput.autocomplete="off"; rotateInput.placeholder="rtsp://camera.example/stream";
    const rotate=document.createElement("button"); rotate.type="button"; rotate.textContent="Test and rotate stream";
    rotate.dataset.configSaves="camera-credentials";
    rotatePanel.cameraSaveConfig=async()=>{await api(`/api/cameras/${encodeURIComponent(camera.id)}/credentials`,{method:"POST",body:JSON.stringify({stream_url:rotateInput.value})});rotateInput.value="";};
    rotate.addEventListener("click",()=>void saveCameraScopes([rotatePanel],{refreshAfter:true}));
    recordingPanel.append(mode,enabledLabel,save);
    motionRollPanel.append(pre,post);
    motionSourcePanel.append(motionUrlLabel,motionUrl,motionUserLabel,motionUser,motionPassLabel,motionPass,fallbackLabel,zoneEditor,zonesAdvanced,sensitivityLabel,sensitivity,configureMotion);
    schedulePanel.append(inheritLabel,scheduleTimezoneLabel,scheduleEditor,addSchedule,loadSchedule,saveSchedule,scheduleAdvanced);
    rotatePanel.append(rotateLabel,rotateInput,rotate);
    card.append(recordingPanel,motionRollPanel,schedulePanel,motionSourcePanel,events,motionEventsResults,aiEvents,aiEventResults,aiConfigure,aiPanel,coverage,coverageList,archive,deleteFootage,rotatePanel);
    const tabs=window.NvrCameraEditor.CAMERA_EDITOR_TABS;
    const tabNav=document.createElement("nav");tabNav.className="camera-editor-tabs";tabNav.setAttribute("role","tablist");tabNav.setAttribute("aria-label",`${camera.name} settings`);
    const panels=new Map();
    for(const tab of tabs){
      const panel=document.createElement("section");panel.className="camera-editor-tab-panel";panel.id=`camera-tab-panel-${camera.id}-${tab.id}`;panel.dataset.cameraTabPanel=tab.id;panel.setAttribute("role","tabpanel");panel.setAttribute("aria-label",tab.label);panel.tabIndex=0;
      const actions=document.createElement("div");actions.className="camera-tab-actions";
      const status=document.createElement("p");status.dataset.cameraTabStatus=tab.id;status.setAttribute("role","status");status.setAttribute("aria-live","polite");status.textContent="Saved";status.dataset.state="saved";
      const saveTab=document.createElement("button");saveTab.type="button";saveTab.textContent="Save";saveTab.dataset.cameraTabSave=tab.id;saveTab.setAttribute("aria-label",`Save ${tab.label} settings`);saveTab.addEventListener("click",()=>void saveCameraTab(tab.id,{refreshAfter:tab.id!=="ai"}));
      const revertTab=document.createElement("button");revertTab.type="button";revertTab.textContent="Revert";revertTab.dataset.cameraTabRevert=tab.id;revertTab.setAttribute("aria-label",`Revert ${tab.label} settings`);revertTab.addEventListener("click",()=>void revertCameraTab(tab.id));
      actions.append(status,saveTab,revertTab);panel.append(actions);panels.set(tab.id,panel);
      const tabButton=document.createElement("button");tabButton.type="button";tabButton.id=`camera-tab-${camera.id}-${tab.id}`;tabButton.textContent=tab.label;tabButton.dataset.cameraTab=tab.id;tabButton.setAttribute("role","tab");tabButton.setAttribute("aria-controls",panel.id);panel.setAttribute("aria-labelledby",tabButton.id);
      tabButton.addEventListener("click",()=>void navigateCameraTab(tab.id));
      tabButton.addEventListener("keydown",event=>{const buttons=[...tabNav.querySelectorAll('[role="tab"]')],index=buttons.indexOf(tabButton);let next=null;if(event.key==="ArrowRight")next=buttons[(index+1)%buttons.length];else if(event.key==="ArrowLeft")next=buttons[(index-1+buttons.length)%buttons.length];else if(event.key==="Home")next=buttons[0];else if(event.key==="End")next=buttons.at(-1);if(next){event.preventDefault();next.focus();void navigateCameraTab(next.dataset.cameraTab);}});
      tabNav.append(tabButton);
    }
    const sectionTab=new Map(tabs.flatMap(tab=>tab.sections.map(section=>[section,tab.id])));
    for(const child of [...card.children].slice(2)){
      if(child===previewSlot)continue;
      const tabId=sectionTab.get(child.dataset.configSection)||child.dataset.cameraTab||"health-history";
      panels.get(tabId)?.append(child);
    }
    card.append(previewSlot,tabNav,...panels.values());
    const activeTab=currentRole==="administrator"?(cameraEditorState.activeTab||"general-connection"):"health-history";
    if(currentRole==="administrator")activateCameraTab(card,activeTab);
    else document.querySelector("#camera-health-history")?.classList.remove("hidden");
    cameraPreviewWorkspace.mount(previewSlot,camera);
    return card;
  }));
}
async function loadSegments(camera) {
  const message = document.querySelector("#camera-message");
  const list = document.querySelector("#segment-list");
  const player = document.querySelector("#playback");
  const selection=cameraEditorState.captureSelection();
  if(selection.cameraId!==camera.id)return;
  message.textContent = `Loading indexed footage for ${camera.name}…`;
  list.replaceChildren(); player.pause(); player.removeAttribute("src"); player.classList.add("hidden");
  try {
    const result = await api(`/api/cameras/${encodeURIComponent(camera.id)}/segments`);
    if(!cameraEditorState.isCurrent(selection))return;
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
  } catch (error) { if(!cameraEditorState.isCurrent(selection))return;message.textContent=error.message; }
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
  const form=event.currentTarget;
  try {
    const result=await api("/api/camera-groups",{method:"POST",body:JSON.stringify({name:document.querySelector("#new-group-name").value})});
    document.querySelector("#bulk-message").textContent=`Created camera group ${result.name}.`;
    clearConfigDraft(form);form.reset();await refreshGroups();
  } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
});
document.querySelector("#load-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  if(!group) { document.querySelector("#schedule-message").textContent="Create or select a camera group first."; return; }
  try {
    const schedule=await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`);
    document.querySelector("#group-schedule-timezone").value=schedule.timezone||"UTC";
    renderScheduleEditor(document.querySelector("#group-schedule-editor"),schedule.intervals||[]);
    document.querySelector("#group-schedule-json").value=JSON.stringify({timezone:schedule.timezone,intervals:schedule.intervals},null,2);
    document.querySelector("#schedule-message").textContent=schedule.configured ? "Group schedule loaded." : "No group schedule is configured.";
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#save-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try {
    if(!group) throw new Error("Create or select a camera group first.");
    const schedule={timezone:document.querySelector("#group-schedule-timezone").value.trim()||"UTC",intervals:readScheduleEditor(document.querySelector("#group-schedule-editor"))};
    const result=await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`,{method:"PUT",body:JSON.stringify(schedule)});
    document.querySelector("#group-schedule-json").value=JSON.stringify(schedule,null,2);
    document.querySelector("#schedule-message").textContent=`Saved ${result.intervals.length} group schedule interval(s).`;
    await refresh();
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#clear-group-schedule").addEventListener("click",async()=>{
  const group=document.querySelector("#schedule-group").value;
  try {
    if(!group) throw new Error("Create or select a camera group first.");
    await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`,{method:"DELETE"});
    document.querySelector("#group-schedule-timezone").value="";renderScheduleEditor(document.querySelector("#group-schedule-editor"),[]);document.querySelector("#group-schedule-json").value="";
    document.querySelector("#schedule-message").textContent="Group schedule removed; assigned cameras now use their camera policy or camera-level override.";
    await refresh();
  } catch(error) { document.querySelector("#schedule-message").textContent=error.message; }
});
document.querySelector("#add-group-schedule-interval").addEventListener("click",()=>{const editor=document.querySelector("#group-schedule-editor");markConfigDraftElement(editor);addScheduleInterval(editor);});
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
  const camera_ids=[...cameraBulkSelection];
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
    cameraBulkSelection.clear();
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
    clearConfigDraft(button.closest("[data-config-section]"));
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
    const zoneEditor=document.querySelector("#group-ai-zones-editor");renderZoneEditor(zoneEditor,config.zones||[]);
    const representative=sidebarGroups.find(item=>item.id===group)?.camera_ids?.[0];if(representative)loadRepresentativeZoneFrame(representative,zoneEditor);
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
    const zones=readZoneEditor(document.querySelector("#group-ai-zones-editor"));document.querySelector("#group-ai-zones").value=JSON.stringify(zones);
    const result=await api(`/api/camera-groups/${encodeURIComponent(group)}/ai`,{method:"PUT",body:JSON.stringify({enabled:document.querySelector("#group-ai-enabled").checked,analysis_fps:Number(document.querySelector("#group-ai-fps").value),confidence_threshold:Number(document.querySelector("#group-ai-confidence").value),use_substream:document.querySelector("#group-ai-substream").checked,zones})});
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
  const form=event.currentTarget;
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
    clearConfigDraft(form);
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
  if(event.target.value==="auto"){
    startLiveViews(activeLiveCameraIds,latestCameras,focusedLiveCameraId,null,activeLiveTileCameraIds,"auto");
    return;
  }
  const size=Number(event.target.value);
  if(activeLiveCameraIds.length>size){event.target.value="auto";document.querySelector("#live-message").textContent=`Auto layout restored to keep all ${activeLiveCameraIds.length} selected cameras visible.`;startLiveViews(activeLiveCameraIds,latestCameras,null,null,activeLiveTileCameraIds,"auto");return;}
  if(activeLiveCameraIds.length)startLiveViews(activeLiveCameraIds,latestCameras,null,size,activeLiveTileCameraIds,"manual");
  else markLiveViewDirty();
});
document.querySelector("#live-stop").addEventListener("click",()=>{stopLiveViews();cachedLiveArrangement=null;document.querySelector("#live-message").textContent="Live viewing stopped.";});
document.querySelector("#live-save-layout").addEventListener("click",async()=>{
  const name=document.querySelector("#live-layout-name").value.trim(), scope=document.querySelector("#live-layout-scope").value;
  const selected=new Set(selectedLiveCameras());
  const layout_mode=document.querySelector("#live-grid-size").value==="auto"?"auto":"manual";
  const size=layout_mode==="auto"?liveGridSizeFor(selected.size):Number(document.querySelector("#live-grid-size").value)||4;
  let tiles;
  if(selected.size>size){document.querySelector("#live-message").textContent=`The ${size}-feed grid cannot save ${selected.size} selected cameras. Choose a larger grid or remove cameras.`;return;}
  if(activeLiveTileCameraIds.length===size){tiles=currentLiveTiles(size).map(id=>id&&selected.has(id)?id:null);for(const id of selected)if(!tiles.includes(id)){const slot=tiles.indexOf(null);if(slot>=0)tiles[slot]=id;}}
  else tiles=[...selected,...Array(size-selected.size).fill(null)];
  const camera_ids=tiles.filter(Boolean);
  try {if(!name)throw new Error("Enter a view name.");if(!camera_ids.length)throw new Error("Select at least one camera.");
    const path=scope==="shared"?"shared-layouts":"layouts";
    const result=await api(`/api/live/${path}/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({camera_ids,tile_camera_ids:tiles,grid_size:size,layout_mode})});
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
window.addEventListener("pagehide",()=>{stopLiveViews();cameraPreviewWorkspace.leave();});
window.addEventListener("resize",()=>requestAnimationFrame(()=>{updateLiveGridLayout();updatePlaybackGridLayout();}));
window.addEventListener("keydown",event=>{
  const editable=event.target instanceof HTMLElement&&(event.target.isContentEditable||["INPUT","TEXTAREA","SELECT"].includes(event.target.tagName));
  if(editable||document.querySelector("#camera-subset-dialog")?.open)return;
  if(event.key==="Escape"&&!document.querySelector("#ops-status-drawer").classList.contains("hidden")){
    event.preventDefault();document.querySelector("#ops-status-close").click();return;
  }
  if(activeScreen==="live"){
    if(event.key==="Escape"&&focusedLiveCameraId){event.preventDefault();startLiveViews(activeLiveCameraIds,latestCameras,null,activeLiveGridSize,[...activeLiveTileCameraIds],activeLiveLayoutMode);}
    if((event.key==="f"||event.key==="F")&&activeLiveTileCameraId){event.preventDefault();startLiveViews(activeLiveCameraIds,latestCameras,focusedLiveCameraId?null:activeLiveTileCameraId,activeLiveGridSize,[...activeLiveTileCameraIds],activeLiveLayoutMode);}
  }
  if(activeScreen==="footage"&&playbackTimeline){
    if(event.code==="Space"){event.preventDefault();playbackIsPlaying?pausePlaybackClock():startPlaybackClock();}
    else if(event.key==="ArrowLeft"||event.key==="ArrowRight"){
      event.preventDefault();
      if(event.shiftKey)navigatePlaybackEvent(event.key==="ArrowLeft"?"previous":"next");
      else{const current=Number(document.querySelector("#playback-cursor").dataset.time)||playbackMilliseconds(playbackTimeline.start);seekSynchronizedPlayback(current+(event.key==="ArrowLeft"?-5000:5000),playbackIsPlaying);}
    }else if(event.key.toLowerCase()==="i")setExportBoundary("start",Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start));
    else if(event.key.toLowerCase()==="o")setExportBoundary("end",Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.end));
  }
});
document.querySelector("#playback-search").addEventListener("click",loadSynchronizedPlayback);
document.querySelector("#playback-grid-size").addEventListener("change",event=>{
  updatePlaybackGridLayout();
  markPlaybackResultsStale();
  savePlaybackWorkspace();
});
document.querySelector("#playback-stop").addEventListener("click",()=>{stopSynchronizedPlayback();document.querySelector("#playback-status").textContent="Playback stopped.";});
document.querySelector("#playback-play").addEventListener("click",startPlaybackClock);
document.querySelector("#playback-pause").addEventListener("click",pausePlaybackClock);
document.querySelector("#playback-prev-frame").addEventListener("click",()=>stepPlaybackFrame("previous").catch(error=>{document.querySelector("#playback-status").textContent=error.message;}));
document.querySelector("#playback-next-frame").addEventListener("click",()=>stepPlaybackFrame("next").catch(error=>{document.querySelector("#playback-status").textContent=error.message;}));
document.querySelector("#playback-prev-event").addEventListener("click",()=>navigatePlaybackEvent("previous"));
document.querySelector("#playback-next-event").addEventListener("click",()=>navigatePlaybackEvent("next"));
document.querySelector("#playback-mark-in").addEventListener("click",()=>setExportBoundary("start",Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline?.start)));
document.querySelector("#playback-mark-out").addEventListener("click",()=>setExportBoundary("end",Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline?.end)));
document.querySelector("#playback-clear-marks").addEventListener("click",()=>{if(!playbackTimeline)return;setExportBoundary("start",playbackMilliseconds(playbackTimeline.start));setExportBoundary("end",playbackMilliseconds(playbackTimeline.end));});
for(const field of ["start","end"]){document.querySelector(`#export-${field}`).addEventListener("change",()=>{updateExportFoldChoices(field);updatePlaybackExportPreview();savePlaybackWorkspace();});document.querySelector(`#export-${field}-fold`).addEventListener("change",()=>{updatePlaybackExportPreview();savePlaybackWorkspace();});}
document.querySelector("#export-camera-selection").addEventListener("change",savePlaybackWorkspace);
document.querySelector("#playback-export").addEventListener("click",requestPlaybackExport);
document.querySelector("#export-include-audio").addEventListener("change",updatePlaybackExportPreview);
document.querySelector("#export-grid-audio-source").addEventListener("change",updatePlaybackExportPreview);
document.querySelectorAll("[data-export-mode]").forEach(input=>input.addEventListener("change",updatePlaybackExportPreview));
document.querySelector("#playback-cursor").addEventListener("input",event=>{
  if(!playbackTimeline)return;
  const start=playbackMilliseconds(playbackTimeline.start),end=playbackMilliseconds(playbackTimeline.end);
  const target=start+(end-start)*Number(event.target.value)/1000;
  const playing=playbackIsPlaying;
  seekSynchronizedPlayback(target,playing);
  savePlaybackWorkspace();
  if(playing)startPlaybackClock();
});
document.querySelector("#playback-rate").addEventListener("change",event=>{playbackViews.forEach(view=>{view.video.playbackRate=Number(event.target.value);});if(playbackIsPlaying)startPlaybackClock();});
window.addEventListener("pagehide",()=>{savePlaybackWorkspace();stopSynchronizedPlayback();});
window.addEventListener("pagehide",()=>{if(exportPollTimer)clearInterval(exportPollTimer);exportPollTimer=null;});
if(incidentPollTimer) clearInterval(incidentPollTimer);
incidentPollTimer=setInterval(()=>{if(currentRole){refreshOperationalInventory();refreshIncidents();}},15000);
if(webhookPollTimer) clearInterval(webhookPollTimer);
webhookPollTimer=setInterval(()=>{if(currentRole==="administrator"){refreshWebhooks();refreshHeartbeatStatus();}},10000);
document.querySelector("#logout").addEventListener("click", async () => { if(!await guardCameraDrafts())return;stopLiveViews();cameraPreviewWorkspace.clearSelection();cachedLiveArrangement=null;workspaceRestored=false;playbackWorkspaceRestored=false;playbackManualChoices.clear();await api("/api/auth/logout", { method:"POST" });currentRole=null;currentUsername="";selectedCameraId=null;cameraEditorState.selectCamera(null);clearCameraHistory();cameraBulkSelection.clear();activeCameraWorkspace="cameras";mobileCameraEditorOpen=false;Object.assign(cameraInventoryFilters,{archive:"active",group:"all",connection:"all",health:"all",sortKey:"name",sortDirection:1,query:""});document.querySelector("#camera-inventory-search").value="";document.querySelectorAll("[data-camera-workspace]").forEach(section=>section.classList.toggle("hidden",section.dataset.cameraWorkspace!=="cameras"));document.querySelectorAll("[data-camera-workspace-button]").forEach(button=>{if(button.dataset.cameraWorkspaceButton==="cameras")button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");});document.querySelector("#camera-admin-layout").dataset.mobileEditor="false";latestSystemStatus=null;latestStorageStatus=null;document.querySelector("#ops-status-drawer").classList.add("hidden");document.querySelector("#ops-status-toggle").setAttribute("aria-expanded","false");document.querySelector("#ops-storage-section").classList.add("hidden");document.querySelector("#incidents-list").replaceChildren();document.querySelector("#webhook-list").replaceChildren();document.querySelector("#webhook-deliveries").replaceChildren();show("login"); });
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
