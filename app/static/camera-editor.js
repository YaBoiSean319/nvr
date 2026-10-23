(function installCameraEditor(root, createModule) {
  const editor = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = editor;
  root.NvrCameraEditor = editor;
})(typeof window === "undefined" ? globalThis : window, function createCameraEditor() {
  const CAMERA_EDITOR_TABS = Object.freeze([
    Object.freeze({ id: "general-connection", label: "General & Connection", sections: Object.freeze(["camera-details", "camera-source", "camera-audio", "camera-credentials"]) }),
    Object.freeze({ id: "recording", label: "Recording", sections: Object.freeze(["camera-recording-policy"]) }),
    Object.freeze({ id: "motion", label: "Motion", sections: Object.freeze(["camera-motion-roll", "camera-motion-source"]) }),
    Object.freeze({ id: "ai", label: "AI", sections: Object.freeze(["camera-ai"]) }),
    Object.freeze({ id: "schedule-retention", label: "Schedule & Retention", sections: Object.freeze(["camera-schedule", "camera-retention"]) }),
    Object.freeze({ id: "health-history", label: "Health & History", sections: Object.freeze([]) }),
  ]);
  const tabIds = new Set(CAMERA_EDITOR_TABS.map(tab => tab.id));

  function createCameraEditorState() {
    let selectedCameraId = null;
    let activeTab = null;
    let generation = 0;

    return {
      get selectedCameraId() { return selectedCameraId; },
      get activeTab() { return activeTab; },
      get generation() { return generation; },
      selectCamera(cameraId) {
        const nextId = cameraId == null ? null : String(cameraId);
        if (nextId === selectedCameraId) return false;
        selectedCameraId = nextId;
        activeTab = nextId ? "general-connection" : null;
        generation += 1;
        return true;
      },
      selectTab(tabId) {
        if (!tabIds.has(tabId) || !selectedCameraId) return false;
        activeTab = tabId;
        return true;
      },
      sectionsForTab(tabId) {
        return CAMERA_EDITOR_TABS.find(tab => tab.id === tabId)?.sections.slice() || [];
      },
      dirtySections(tabId, dirtySectionIds) {
        const dirty = new Set(dirtySectionIds || []);
        return this.sectionsForTab(tabId).filter(sectionId => dirty.has(sectionId));
      },
      allDirtySections(dirtySectionIds) {
        const dirty = new Set(dirtySectionIds || []);
        return CAMERA_EDITOR_TABS.flatMap(tab => tab.sections.filter(sectionId => dirty.has(sectionId)));
      },
      captureSelection() {
        return Object.freeze({ cameraId: selectedCameraId, generation });
      },
      isCurrent(selection) {
        return Boolean(selection && selection.cameraId === selectedCameraId && selection.generation === generation);
      },
    };
  }

  async function resolveDirtyNavigation(decision, { save, discard } = {}) {
    if (decision === "cancel") return false;
    if (decision === "discard") {
      await discard?.();
      return true;
    }
    if (decision === "save") return Boolean(await save?.());
    return false;
  }

  function createCameraEditorView(services = {}) {
    const { api, formatDateTime, refresh } = services;
    const { saveCameraScopes, guardCameraDrafts, markConfigDraftElement } = services.configuration || {};
    const { renderZoneEditor, loadRepresentativeZoneFrame, readZoneEditor } = services.motionZones || {};
    const { addScheduleInterval, renderScheduleEditor, readScheduleEditor } = services.schedule || {};
    const { saveCameraTab, revertCameraTab, navigateCameraTab, activateCameraTab } = services.tabs || {};
    const { cameraPreviewWorkspace, loadSegments } = services.media || {};
    const {
      getState: getLiveViewState = () => ({ tiles: [], ids: [], size: 1, focused: null }),
      getCameras: getLatestCameras = () => [],
      start: startLiveViews = () => {},
    } = services.liveViews || {};

    function render(camera, { role: currentRole, selectedCameraId, editorState: cameraEditorState } = {}) {
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
      const sensitivity=document.createElement("input"); sensitivity.id="camera-editor-motion-sensitivity"; sensitivityLabel.htmlFor=sensitivity.id; sensitivity.type="number"; sensitivity.min="1"; sensitivity.max="100"; sensitivity.value=camera.motion_sensitivity||50;
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
        const { tiles, ids, size, focused } = getLiveViewState();
        if(!camera.archived_at&&!window.confirm(`Archive ${camera.name}? Collection stops, and its recordings remain available for search and export.`))return;
        try {
          if(camera.archived_at)await api(`/api/cameras/${encodeURIComponent(camera.id)}/restore`,{method:"POST"});
          else await api(`/api/cameras/${encodeURIComponent(camera.id)}`,{method:"DELETE"});
          if(!camera.archived_at)cameraPreviewWorkspace.close();
          await refresh();
          if(ids.includes(camera.id))startLiveViews(ids,getLatestCameras(),focused,size,tiles);
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
      const tabs=CAMERA_EDITOR_TABS;
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
    }
    return { render };
  }

  return { CAMERA_EDITOR_TABS, createCameraEditorState, resolveDirtyNavigation, createCameraEditorView };
});
