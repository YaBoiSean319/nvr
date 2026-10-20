const sections = ["setup", "login", "status"];
const message = document.querySelector("#message");
let currentRole = null;
let importCanApply = false;
let lastPreviewCsv = "";
function show(name) { for (const id of sections) document.querySelector(`#${id}`).classList.toggle("hidden", id !== name); }
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
async function refresh() {
  message.textContent = "";
  try {
    const who = await api("/api/auth/me");
    currentRole = who.role;
    const admin = currentRole === "administrator";
    document.querySelector("#status h2").textContent = admin ? `System status · ${who.username}` : `Cameras · ${who.username}`;
    document.querySelector("#status-list").classList.toggle("hidden", !admin);
    document.querySelector("#session-admin").classList.toggle("hidden", !admin);
    document.querySelector("#camera-form").classList.toggle("hidden", !admin);
    document.querySelector("#discovery-admin").classList.toggle("hidden", !admin);
    document.querySelector("#bulk-admin").classList.toggle("hidden", !admin);
    if (admin) {
      const data = await api("/api/system/status");
      const rows = [["Version", data.version], ["Database", data.database], ["Administrators", data.configured_administrators], ["Storage available", data.storage.available_bytes == null ? "Unavailable" : `${(data.storage.available_bytes / 1073741824).toFixed(1)} GiB`]];
      document.querySelector("#status-list").replaceChildren(...rows.flatMap(([k,v]) => { const dt=document.createElement("dt"), dd=document.createElement("dd"); dt.textContent=k; dd.textContent=v; return [dt,dd]; }));
      await refreshSessions();
    }
    const inventory = await api("/api/cameras");
    if (admin) await refreshGroups();
    renderCameras(inventory.cameras);
    if (admin) await refreshDiscovery();
    show("status");
  } catch (error) {
    if (error.message === "Authentication required" || error.message === "Session expired") {
      currentRole = null; await showLogin();
    } else { message.textContent = error.message; }
  }
}
async function refreshGroups() {
  const {groups}=await api("/api/camera-groups");
  const select=document.querySelector("#bulk-group");
  const options=[new Option("Leave groups unchanged","__unchanged__"),new Option("Remove from group","")];
  for (const group of groups) options.push(new Option(group.name,group.name));
  select.replaceChildren(...options);
}
async function refreshDiscovery() {
  const [{candidates}, inventory] = await Promise.all([api("/api/discovery/candidates"), api("/api/cameras")]);
  const target=document.querySelector("#discovery-candidates");
  if (!candidates.length) { target.textContent="No discovered cameras awaiting review."; return; }
  target.replaceChildren(...candidates.map(candidate => {
    const card=document.createElement("article");
    const title=document.createElement("h4");
    title.textContent=`ONVIF device ${candidate.device_uuid}`;
    const state=document.createElement("p");
    state.textContent=candidate.linked_camera_id ? `Already linked to camera ${candidate.linked_camera_id}` : `First seen ${new Date(candidate.first_seen_at).toLocaleString()} · Last seen ${new Date(candidate.last_seen_at).toLocaleString()}`;
    const services=document.createElement("p"); services.textContent=`Device service: ${candidate.service_urls.join(", ") || "unavailable"}`;
    const capabilities=document.createElement("p");
    capabilities.textContent=`Capabilities · ${Object.entries(candidate.capabilities).map(([name,result])=>`${name}: ${result}`).join(" · ")}`;
    card.append(title,state,services,capabilities);
    if (!candidate.linked_camera_id) {
      const nameLabel=document.createElement("label"); nameLabel.textContent="Camera name";
      const name=document.createElement("input"); name.maxLength=120; name.required=true; name.value=`Camera ${candidate.device_uuid.slice(0,8)}`;
      const streamLabel=document.createElement("label"); streamLabel.textContent="RTSP or RTSPS stream URL";
      const stream=document.createElement("input"); stream.type="url"; stream.autocomplete="off"; stream.required=true; stream.placeholder="rtsp://user:password@camera/stream";
      const add=document.createElement("button"); add.type="button"; add.textContent="Test and add camera";
      add.addEventListener("click",async()=>{
        try {
          const camera=await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/add`,{method:"POST",body:JSON.stringify({name:name.value,stream_url:stream.value,recording_mode:"motion"})});
          document.querySelector("#discovery-message").textContent=`${camera.name} added. Review and explicitly select a supported recording mode.`;
          await refresh();
        } catch(error) { document.querySelector("#discovery-message").textContent=error.message; }
      });
      card.append(nameLabel,name,streamLabel,stream,add);
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
    summary.textContent = `${camera.group_name ? `${camera.group_name} · ` : ""}${camera.recording_mode} · ${camera.recording_status} · ${video.codec || "unknown codec"}${video.width && video.height ? ` ${video.width}×${video.height}` : ""}`;
    card.append(title, summary);
    if (camera.last_error) { const note=document.createElement("p"); note.textContent=camera.last_error; card.append(note); }
    const button = document.createElement("button"); button.type="button"; button.textContent="Find recorded footage";
    button.addEventListener("click", () => loadSegments(camera)); card.append(button);
    if (currentRole !== "administrator") return card;
    const mode=document.createElement("select"); mode.setAttribute("aria-label",`Recording mode for ${camera.name}`);
    for (const [value,label] of [["motion","Motion (not available yet)"],["continuous","Continuous"],["disabled","Disabled"]]) {
      const option=document.createElement("option"); option.value=value; option.textContent=label; mode.append(option);
    }
    mode.value=camera.recording_mode;
    const save=document.createElement("button"); save.type="button"; save.textContent="Apply recording mode";
    save.addEventListener("click", async () => {
      try {
        await api(`/api/cameras/${encodeURIComponent(camera.id)}`, { method:"PATCH", body:JSON.stringify({recording_mode:mode.value}) });
        document.querySelector("#camera-message").textContent=`${camera.name}: recording mode updated.`;
        await refresh();
      } catch (error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const remove=document.createElement("button"); remove.type="button"; remove.textContent="Remove camera";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Remove ${camera.name} and its saved footage?`)) return;
      try { await api(`/api/cameras/${encodeURIComponent(camera.id)}`, {method:"DELETE"}); await refresh(); }
      catch (error) { document.querySelector("#camera-message").textContent=error.message; }
    });
    const rotateLabel=document.createElement("label"); rotateLabel.textContent="Update stream credentials or URL";
    const rotateInput=document.createElement("input"); rotateInput.type="url"; rotateInput.autocomplete="off"; rotateInput.placeholder="rtsp://new-user:new-password@camera/stream";
    const rotate=document.createElement("button"); rotate.type="button"; rotate.textContent="Test and rotate stream";
    rotate.addEventListener("click",async()=>{
      try {
        await api(`/api/cameras/${encodeURIComponent(camera.id)}/credentials`,{method:"POST",body:JSON.stringify({stream_url:rotateInput.value})});
        document.querySelector("#bulk-message").textContent=`${camera.name}: updated stream validated and the recorder is reconnecting.`;
        rotateInput.value=""; await refresh();
      } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
    });
    card.append(mode,save,remove,rotateLabel,rotateInput,rotate);
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
      const button=document.createElement("button"); button.type="button";
      button.textContent=`${new Date(segment.starts_at).toLocaleString()} – ${new Date(segment.ends_at).toLocaleTimeString()} · ${(segment.size_bytes/1048576).toFixed(1)} MiB`;
      button.addEventListener("click", () => { player.src=segment.media_url; player.classList.remove("hidden"); player.play().catch(() => { message.textContent="Interval loaded. Press Play to start playback."; }); });
      return button;
    }));
  } catch (error) { message.textContent=error.message; }
}
document.querySelector("#test-camera").addEventListener("click", async () => {
  const message=document.querySelector("#camera-message"); message.textContent="Testing RTSP video…";
  try {
    const result=await api("/api/cameras/test", { method:"POST", body:JSON.stringify({ stream_url:document.querySelector("#stream-url").value }) });
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
document.querySelector("#group-form").addEventListener("submit",async event=>{
  event.preventDefault();
  try {
    const result=await api("/api/camera-groups",{method:"POST",body:JSON.stringify({name:document.querySelector("#new-group-name").value})});
    document.querySelector("#bulk-message").textContent=`Created camera group ${result.name}.`;
    event.currentTarget.reset(); await refreshGroups();
  } catch(error) { document.querySelector("#bulk-message").textContent=error.message; }
});
document.querySelector("#apply-bulk").addEventListener("click",async()=>{
  const camera_ids=[...document.querySelectorAll("[data-camera-select]:checked")].map(input=>input.value);
  const payload={camera_ids};
  const group=document.querySelector("#bulk-group").value;
  const mode=document.querySelector("#bulk-recording-mode").value;
  if(group!=="__unchanged__") payload.group_name=group || null;
  if(mode) payload.recording_mode=mode;
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
    const result=await api("/api/cameras", { method:"POST", body:JSON.stringify({
      name:document.querySelector("#camera-name").value,
      stream_url:document.querySelector("#stream-url").value,
      recording_mode:document.querySelector("#recording-mode").value
    }) });
    document.querySelector("#camera-form").reset();
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
document.querySelector("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method:"POST" }); show("login"); });
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
