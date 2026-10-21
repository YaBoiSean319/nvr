const sections = ["setup", "login", "status"];
const message = document.querySelector("#message");
let currentRole = null;
let importCanApply = false;
let lastPreviewCsv = "";
let liveViews = [];
let liveStatusTimer = null;
let latestCameras = [];
let playbackTimeline = null;
let playbackViews = [];
let playbackMaster = null;
let playbackSyncGuard = false;
let exportPollTimer = null;
let incidentPollTimer = null;
function show(name) { if(name!=="status") stopSynchronizedPlayback(); for (const id of sections) document.querySelector(`#${id}`).classList.toggle("hidden", id !== name); }
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
    document.querySelector("#status h2").textContent = admin ? `System status · ${who.username}` : `Cameras · ${who.username}`;
    document.querySelector("#status-list").classList.toggle("hidden", !admin);
    document.querySelector("#session-admin").classList.toggle("hidden", !admin);
    document.querySelector("#diagnostics-admin").classList.toggle("hidden", !admin);
    document.querySelector("#camera-form").classList.toggle("hidden", !admin);
    document.querySelector("#discovery-admin").classList.toggle("hidden", !admin);
    document.querySelector("#bulk-admin").classList.toggle("hidden", !admin);
    if (admin) {
      const data = await api("/api/system/status");
      const rows = [["Version", data.version], ["Database", data.database], ["Administrators", data.configured_administrators], ["Storage available", data.storage.available_bytes == null ? "Unavailable" : `${(data.storage.available_bytes / 1073741824).toFixed(1)} GiB`]];
      document.querySelector("#status-list").replaceChildren(...rows.flatMap(([k,v]) => { const dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=k; dd.textContent=v; return [dt,dd]; }));
      await refreshSessions();
      await refreshDiagnostics();
    }
    const inventory = await api("/api/cameras");
    if (admin) await refreshGroups();
    latestCameras = inventory.cameras;
    await refreshIncidents();
    renderCameras(inventory.cameras);
    renderLiveCameraPicker(inventory.cameras);
    renderPlaybackCameraPicker(inventory.cameras);
    await refreshLiveLayouts();
    await refreshExports();
    if (admin) await refreshDiscovery();
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
    const {layouts}=await api("/api/live/layouts");
    const select=document.querySelector("#live-layouts"), previous=select.value;
    select.replaceChildren(new Option("Choose a saved layout",""),...layouts.map(layout=>new Option(layout.name,layout.name)));
    if(layouts.some(layout=>layout.name===previous)) select.value=previous;
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
      details.textContent=`First seen ${new Date(incident.first_seen_at).toLocaleString()} · Last seen ${new Date(incident.last_seen_at).toLocaleString()} · Recurrences ${Math.max(0,incident.recurrence_count-1)} · ${incident.events.length} history events`;
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
function stopLiveViews() {
  if(liveStatusTimer) clearInterval(liveStatusTimer);
  liveStatusTimer=null;
  for(const view of liveViews) { view.controller.abort(); if(view.url) URL.revokeObjectURL(view.url); }
  liveViews=[];
  document.querySelector("#live-grid").replaceChildren();
}
function selectedLiveCameras() { return [...document.querySelectorAll("[data-live-camera]:checked")].map(input=>input.value); }
function startLiveViews(cameraIds,cameras,focusedCameraId=null) {
  if(!cameraIds.length || cameraIds.length>8) { document.querySelector("#live-message").textContent="Choose between one and eight cameras."; return; }
  stopLiveViews();
  const focusQuality=document.querySelector("#live-quality").value;
  const grid=document.querySelector("#live-grid"), liveMessage=document.querySelector("#live-message");
  const displayed=focusedCameraId?[focusedCameraId]:cameraIds;
  for(const cameraId of displayed) {
    const camera=cameras.find(item=>item.id===cameraId); if(!camera) continue;
    const tile=document.createElement("article"); tile.className="live-tile";
    const title=document.createElement("strong"); title.textContent=camera.name;
    const state=document.createElement("span"); state.className="live-state"; state.textContent="Loading";
    const image=document.createElement("img"); image.alt=`Live video from ${camera.name}`;
    const focus=document.createElement("button"); focus.type="button"; focus.textContent=focusedCameraId?"Back to grid":"Focus";
    focus.addEventListener("click",()=>startLiveViews(cameraIds,cameras,focusedCameraId?null:cameraId));
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
    tile.append(image); grid.append(tile);
    const view={cameraId,controller:new AbortController(),image,state,url:null,lastFrame:0}; liveViews.push(view);
    consumeLiveStream(view,"low",focusQuality).catch(error=>{if(!view.controller.signal.aborted){state.textContent="Lost";liveMessage.textContent=`${camera.name}: ${error.message}`;}});
  }
  liveStatusTimer=setInterval(async()=>{
    for(const view of liveViews) {
      try {
        const status=await api(`/api/cameras/${encodeURIComponent(view.cameraId)}/live/status`);
        if(!view.controller.signal.aborted) view.state.textContent=status.state==="live"?"Live":status.active_viewers?"Stale":"Lost";
      } catch { if(!view.controller.signal.aborted) view.state.textContent="Lost"; }
    }
  },5000);
}
async function consumeLiveStream(view,gridQuality,focusQuality) {
  const quality=view.image.closest(".live-tile")?.querySelector("button")?.textContent==="Back to grid"?focusQuality:gridQuality;
  const response=await fetch(`/api/cameras/${encodeURIComponent(view.cameraId)}/live?quality=${quality}`,{credentials:"same-origin",signal:view.controller.signal});
  if(!response.ok) { const body=await response.json().catch(()=>({})); throw new Error(body.detail||"Live view could not start"); }
  if(!response.body) throw new Error("This browser cannot read the live stream");
  const reader=response.body.getReader(); let buffer=new Uint8Array(0);
  while(!view.controller.signal.aborted) {
    const {value,done}=await reader.read();
    if(done) break;
    const merged=new Uint8Array(buffer.length+value.length); merged.set(buffer); merged.set(value,buffer.length); buffer=merged;
    while(true) {
      let start=-1,end=-1;
      for(let i=0;i<buffer.length-1;i++) if(buffer[i]===255&&buffer[i+1]===216){start=i;break;}
      if(start<0){buffer=buffer.slice(Math.max(0,buffer.length-1));break;}
      for(let i=start+2;i<buffer.length-1;i++) if(buffer[i]===255&&buffer[i+1]===217){end=i+2;break;}
      if(end<0){buffer=buffer.slice(start);if(buffer.length>4*1024*1024)throw new Error("Video frame exceeded size limit");break;}
      const frame=buffer.slice(start,end);buffer=buffer.slice(end);
      const url=URL.createObjectURL(new Blob([frame],{type:"image/jpeg"}));
      if(view.url) URL.revokeObjectURL(view.url); view.url=url; view.image.src=url; view.lastFrame=Date.now(); view.state.textContent="Live";
    }
    if(buffer.length>4*1024*1024) throw new Error("Live stream exceeded the frame buffer limit");
  }
  if(!view.controller.signal.aborted) view.state.textContent="Lost";
}
function renderPlaybackCameraPicker(cameras) {
  const target=document.querySelector("#playback-camera-picker");
  target.replaceChildren(...cameras.map(camera=>{
    const label=document.createElement("label"); label.textContent=`${camera.name} `;
    const input=document.createElement("input"); input.type="checkbox"; input.dataset.playbackCamera="true"; input.value=camera.id;
    label.append(input); return label;
  }));
}
function stopSynchronizedPlayback() {
  playbackViews.forEach(view=>{view.video.pause();view.video.removeAttribute("src");view.video.load();});
  playbackViews=[]; playbackTimeline=null; playbackMaster=null;
  document.querySelector("#playback-grid")?.replaceChildren();
  const cursor=document.querySelector("#playback-cursor"); if(cursor) cursor.disabled=true;
}
function playbackMilliseconds(value) { return new Date(value).getTime(); }
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
  if(playbackTimeline) label.textContent=`Shared time ${new Date(Number(document.querySelector("#playback-cursor").dataset.time||playbackTimeline.start)).toLocaleString()} · measured playback drift ${Math.round(drift)} ms`;
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
  const track=document.createElement("div"); track.className="playback-track"; track.setAttribute("aria-label",`Motion and coverage timeline for ${view.name}`);
  const start=playbackMilliseconds(playbackTimeline.start), span=playbackMilliseconds(playbackTimeline.end)-start;
  for(const gap of view.gaps){const mark=document.createElement("button");mark.type="button";mark.className=`playback-gap ${gap.state}`;const left=(playbackMilliseconds(gap.starts_at)-start)/span*100,width=(playbackMilliseconds(gap.ends_at)-playbackMilliseconds(gap.starts_at))/span*100;mark.style.left=`${left}%`;mark.style.width=`${Math.max(.4,width)}%`;mark.title=`${gap.state}: ${gap.reason}`;mark.setAttribute("aria-label",mark.title);mark.addEventListener("click",()=>seekSynchronizedPlayback(playbackMilliseconds(gap.starts_at)));track.append(mark);}
  for(const event of view.motion_events){const marker=document.createElement("button");marker.type="button";marker.className="playback-motion";marker.style.left=`${(playbackMilliseconds(event.source_event_at)-start)/span*100}%`;marker.title=`Motion · ${new Date(event.source_event_at).toLocaleString()} · ${event.status}`;marker.setAttribute("aria-label",marker.title);marker.addEventListener("click",()=>seekSynchronizedPlayback(playbackMilliseconds(event.source_event_at)));track.append(marker);}
  tile.append(track);
  view.video=video;view.state=state;view.tile=tile;
  video.addEventListener("play",()=>{if(playbackSyncGuard)return;playbackMaster=view;const time=playbackAbsTime(view);if(time!==null)seekSynchronizedPlayback(time,true,view);});
  video.addEventListener("pause",()=>{if(playbackSyncGuard)return;playbackViews.forEach(other=>{if(other!==view)other.video.pause();});});
  video.addEventListener("seeking",()=>{if(playbackSyncGuard)return;const time=playbackAbsTime(view);if(time!==null)seekSynchronizedPlayback(time,!view.video.paused,view);});
  video.addEventListener("timeupdate",()=>{if(playbackSyncGuard||playbackMaster!==view)return;const time=playbackAbsTime(view);if(time===null)return;playbackSetCursor(time);playbackSyncGuard=true;playbackViews.filter(other=>other!==view).forEach(other=>playbackSetSegment(other,time,!view.video.paused));playbackSyncGuard=false;updatePlaybackDrift();});
  return tile;
}
async function loadSynchronizedPlayback() {
  stopSynchronizedPlayback();
  const camera_ids=[...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value);
  const startInput=document.querySelector("#playback-start").value,endInput=document.querySelector("#playback-end").value;
  const label=document.querySelector("#playback-status");
  try {
    if(!camera_ids.length||camera_ids.length>8)throw new Error("Choose between one and eight cameras.");
    if(!startInput||!endInput)throw new Error("Enter both a start and end time.");
    const result=await api("/api/playback/timeline",{method:"POST",body:JSON.stringify({camera_ids,start:new Date(startInput).toISOString(),end:new Date(endInput).toISOString()})});
    playbackTimeline=result;const cursor=document.querySelector("#playback-cursor");cursor.disabled=false;cursor.value="0";cursor.dataset.time=String(playbackMilliseconds(result.start));
    const grid=document.querySelector("#playback-grid");
    playbackViews=result.cameras.map(camera=>({cameraId:camera.camera_id,name:camera.name,segments:camera.segments,gaps:camera.gaps,motion_events:camera.motion_events,coverage:camera.coverage,video:null,state:null,segment:null}));
    grid.replaceChildren(...playbackViews.map(renderPlaybackCamera));
    seekSynchronizedPlayback(playbackMilliseconds(result.start));
    label.textContent=`Loaded ${result.cameras.length} camera timeline(s). Recorded footage, motion markers, and known gaps are shown below.`;
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
      state.textContent=`${job.id.slice(0,8)} · ${job.status}${job.cancel_requested?" · cancelling":""}${job.error?` · ${job.error}`:""}`;
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
  const camera_ids=[...document.querySelectorAll("[data-playback-camera]:checked")].map(input=>input.value);
  const start=document.querySelector("#playback-start").value,end=document.querySelector("#playback-end").value;
  try {
    if(!camera_ids.length||camera_ids.length>8)throw new Error("Choose between one and eight cameras.");
    if(!start||!end)throw new Error("Enter both a start and end time.");
    button.disabled=true;message.textContent="Submitting export…";
    const job=await api("/api/exports",{method:"POST",body:JSON.stringify({camera_ids,start:new Date(start).toISOString(),end:new Date(end).toISOString()})});
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
    ? `Capture is on until ${new Date(state.capture_until).toLocaleString()} · ${state.event_count} event(s) collected.`
    : `${state.event_count} event(s) available for download. Capture is off.`;
}
async function refreshGroups() {
  const {groups}=await api("/api/camera-groups");
  const select=document.querySelector("#bulk-group");
  const options=[new Option("Leave groups unchanged","__unchanged__"),new Option("Remove from group","")];
  for (const group of groups) options.push(new Option(group.name,group.name));
  select.replaceChildren(...options);
  const scheduleSelect=document.querySelector("#schedule-group");
  scheduleSelect.replaceChildren(...(groups.length ? groups.map(group=>new Option(group.name,group.id)) : [new Option("Create a group first","")]));
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
    state.textContent=candidate.linked_camera_id ? `Already linked to camera ${candidate.linked_camera_id}` : `First seen ${new Date(candidate.first_seen_at).toLocaleString()} · Last seen ${new Date(candidate.last_seen_at).toLocaleString()}`;
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
    text.textContent=`${session.username} (${session.role}) · expires ${new Date(session.expires_at).toLocaleString()} `;
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
    summary.textContent = (camera.group_name ? camera.group_name + " · " : "") + "storage " + (camera.storage_volume_id||"default") + " · " + (camera.recording_enabled ? "recording enabled" : "recording disabled") + " · effective " + effectiveMode + " (" + policyLabel + ") · " + camera.recording_status + " · coverage " + (healthSummary.state||"unknown") + (healthSummary.detail ? " — " + healthSummary.detail : "") + (healthSummary.last_video_at ? " · video seen " + healthSummary.last_video_at : "") + (healthSummary.last_footage_write_at ? " · footage written " + healthSummary.last_footage_write_at : "") + (camera.recording_mode === "motion" ? " · event source " + camera.motion_source_status + " · server fallback " + (camera.motion_fallback_enabled ? camera.server_motion_status : "disabled") : "") + " · " + (video.codec || "unknown codec") + (video.width && video.height ? " " + video.width + "×" + video.height : "");
    const retentionSummary=camera.retention_coverage||{};
    summary.textContent += ` · retention ${camera.retention_days} days (${camera.retention_policy_source||"default"}; ${retentionSummary.achieved_days||0} days achieved` + (retentionSummary.projected_days_at_current_activity == null ? ")" : `; ${retentionSummary.projected_days_at_current_activity} days projected at current activity)`);
    card.append(title, summary);
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
          row.textContent=new Date(event.source_event_at).toLocaleString()+" · "+event.source+" · "+event.status+" · pre-roll "+(event.pre_roll_complete ? "complete" : "incomplete/unknown")+(event.media_url ? " · playable clip available" : "");
          if(event.media_url) { const play=document.createElement("button"); play.type="button"; play.textContent="Play"; play.addEventListener("click",()=>{const player=document.querySelector("#playback");player.src=event.media_url;player.classList.remove("hidden");player.play().catch(()=>{});}); row.append(play); }
          return row;
        }));
      } catch(error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const coverage=document.createElement("button"); coverage.type="button"; coverage.textContent="View coverage history";
    const coverageList=document.createElement("ul");
    coverage.addEventListener("click",async()=>{
      try {
        const result=await api(`/api/cameras/${encodeURIComponent(camera.id)}/coverage`);
        coverageList.replaceChildren(...result.intervals.map(interval=>{
          const item=document.createElement("li");
          const end=interval.ends_at ? new Date(interval.ends_at).toLocaleString() : "ongoing";
          item.textContent=`${interval.coverage_kind}: ${interval.reason} · ${new Date(interval.starts_at).toLocaleString()} to ${end}`;
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
    card.append(mode,enabledLabel,pre,post,save,scheduleLabel,inheritLabel,scheduleText,loadSchedule,saveSchedule,motionUrlLabel,motionUrl,motionUserLabel,motionUser,motionPassLabel,motionPass,fallbackLabel,zonesLabel,zones,sensitivityLabel,sensitivity,configureMotion,events,coverage,coverageList,remove,rotateLabel,rotateInput,rotate);
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
      play.textContent=`Play ${new Date(segment.starts_at).toLocaleString()} – ${new Date(segment.ends_at).toLocaleTimeString()} · ${(segment.size_bytes/1048576).toFixed(1)} MiB`;
      play.addEventListener("click", () => { player.src=segment.media_url; player.classList.remove("hidden"); player.play().catch(() => { message.textContent="Interval loaded. Press Play to start playback."; }); });
      item.append(play);
      if(segment.preserved) {
        const held=document.createElement("span"); held.textContent=segment.hold_expires_at ? ` Preserved until ${new Date(segment.hold_expires_at).toLocaleString()}.` : " Preserved until an administrator releases the hold."; item.append(held);
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
document.querySelector("#live-stop").addEventListener("click",()=>{stopLiveViews();document.querySelector("#live-message").textContent="Live viewing stopped.";});
document.querySelector("#live-save-layout").addEventListener("click",async()=>{
  const name=document.querySelector("#live-layout-name").value.trim(), camera_ids=selectedLiveCameras();
  try {if(!name)throw new Error("Enter a layout name.");const result=await api(`/api/live/layouts/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({camera_ids})});document.querySelector("#live-message").textContent=`Saved “${result.name}” for your account.`;await refreshLiveLayouts();document.querySelector("#live-layouts").value=result.name;}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
document.querySelector("#live-load-layout").addEventListener("click",async()=>{
  const name=document.querySelector("#live-layouts").value;
  try {if(!name)throw new Error("Choose a saved layout.");const {layouts}=await api("/api/live/layouts");const layout=layouts.find(item=>item.name===name);if(!layout)throw new Error("Saved layout is unavailable.");for(const input of document.querySelectorAll("[data-live-camera]"))input.checked=layout.camera_ids.includes(input.value);startLiveViews(layout.camera_ids,latestCameras);}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
document.querySelector("#live-delete-layout").addEventListener("click",async()=>{
  const name=document.querySelector("#live-layouts").value;
  try {if(!name)throw new Error("Choose a saved layout.");await api(`/api/live/layouts/${encodeURIComponent(name)}`,{method:"DELETE"});document.querySelector("#live-message").textContent=`Deleted “${name}”.`;await refreshLiveLayouts();}
  catch(error){document.querySelector("#live-message").textContent=error.message;}
});
window.addEventListener("pagehide",stopLiveViews);
document.querySelector("#playback-search").addEventListener("click",loadSynchronizedPlayback);
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
document.querySelector("#logout").addEventListener("click", async () => { stopLiveViews(); await api("/api/auth/logout", { method:"POST" }); currentRole=null; document.querySelector("#incidents-list").replaceChildren(); show("login"); });
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
