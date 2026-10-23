(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.NvrCameraGroups = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  const GROUP_POLICY_TABS = Object.freeze([
    Object.freeze({ id: "general", label: "Group & Members" }),
    Object.freeze({ id: "recording", label: "Schedule & Retention" }),
    Object.freeze({ id: "storage", label: "Storage" }),
    Object.freeze({ id: "ai", label: "AI" }),
  ]);

  function createGroupWorkspaceState() {
    let groups = [];
    let selectedGroupId = null;
    let activeTab = GROUP_POLICY_TABS[0].id;
    let query = "";
    let generation = 0;

    return {
      get groups() { return groups.slice(); },
      get selectedGroupId() { return selectedGroupId; },
      get activeTab() { return activeTab; },
      get query() { return query; },
      get generation() { return generation; },
      setGroups(nextGroups) {
        groups = Array.isArray(nextGroups) ? nextGroups.slice() : [];
        if (selectedGroupId && !groups.some((group) => group.id === selectedGroupId)) {
          selectedGroupId = null;
          generation += 1;
          activeTab = GROUP_POLICY_TABS[0].id;
        }
        return this.groups;
      },
      setQuery(value) {
        query = String(value || "").trim().toLocaleLowerCase();
        return this.visibleGroups();
      },
      visibleGroups() {
        if (!query) return groups.slice();
        return groups.filter((group) => String(group.name || "").toLocaleLowerCase().includes(query));
      },
      selectGroup(groupId) {
        if (!groups.some((group) => group.id === groupId)) return false;
        if (selectedGroupId !== groupId) {
          generation += 1;
          activeTab = GROUP_POLICY_TABS[0].id;
        }
        selectedGroupId = groupId;
        return true;
      },
      clearSelection() {
        if (selectedGroupId !== null) generation += 1;
        selectedGroupId = null;
        activeTab = GROUP_POLICY_TABS[0].id;
      },
      selectedGroup() {
        return groups.find((group) => group.id === selectedGroupId) || null;
      },
      selectTab(tabId) {
        if (!GROUP_POLICY_TABS.some((tab) => tab.id === tabId)) return false;
        activeTab = tabId;
        return true;
      },
      captureSelection() {
        return Object.freeze({ groupId: selectedGroupId, generation });
      },
      isCurrent(selection) {
        return Boolean(selection && selection.groupId === selectedGroupId && selection.generation === generation);
      },
    };
  }

  const GROUP_POLICY_SECTIONS={general:["group-name","group-members"],recording:["group-schedule","group-retention"],storage:["group-storage"],ai:["group-ai"]};
  const GROUP_SECTION_TAB=Object.fromEntries(Object.entries(GROUP_POLICY_SECTIONS).flatMap(([tab,sections])=>sections.map(section=>[section,tab])));

  function mountWorkspace(root, services = {}) {
    if (!root) return null;
    const {
      api, refreshApp, getRole = () => null, getCameras = () => [], setSidebarGroups = () => {},
      clearConfigDraft, markConfigDraftElement, askCameraDirtyDecision,
      renderScheduleEditor, readScheduleEditor, renderZoneEditor, readZoneEditor,
      loadRepresentativeZoneFrame, addScheduleInterval,
    } = services;
    const groupWorkspaceState=createGroupWorkspaceState();
    let groupWorkspaceInitialized=false;
    let groupWorkspaceSaveInProgress=false;
    let groupPolicyLoading=false;
    let groupPolicyLoadFailures=new Set();
    let unavailableGroupId=null;
function setupGroupPolicyWorkspace(){
  if(groupWorkspaceInitialized)return;
  if(!root)return;
  const heading=root.querySelector("#bulk-heading"),groupForm=root.querySelector("#group-form"),instructions=groupForm?.nextElementSibling,list=root.querySelector("#group-management-list");
  const schedule=root.querySelector('[data-config-section="group-schedule"]'),retention=root.querySelector('[data-config-section="group-retention"]'),storage=root.querySelector('[data-config-section="group-storage"]'),ai=root.querySelector('[data-config-section="group-ai"]');
  const runtime=root.querySelector('[data-config-section="ai-runtime"]'),scheduleMessage=root.querySelector("#schedule-message"),aiMessage=root.querySelector("#ai-group-message");
  const layout=document.createElement("div");layout.id="group-policy-layout";layout.className="group-policy-layout";
  const listPane=document.createElement("aside");listPane.className="group-list-pane";listPane.setAttribute("aria-labelledby","group-list-heading");
  const listHeading=document.createElement("h4");listHeading.id="group-list-heading";listHeading.textContent="Camera groups";
  const searchLabel=document.createElement("label");searchLabel.htmlFor="group-search";searchLabel.textContent="Search groups";
  const search=document.createElement("input");search.type="search";search.id="group-search";search.autocomplete="off";search.placeholder="Group name";
  const createMessage=document.createElement("p");createMessage.id="group-create-message";createMessage.setAttribute("role","status");createMessage.setAttribute("aria-live","polite");
  const filteredNotice=document.createElement("p");filteredNotice.id="group-filter-notice";filteredNotice.className="group-filter-notice hidden";
  const showGroup=document.createElement("button");showGroup.type="button";showGroup.textContent="Show selected group in list";showGroup.addEventListener("click",()=>{search.value="";groupWorkspaceState.setQuery("");renderGroupManagement(groupWorkspaceState.groups);document.querySelector(`#group-management-list button[data-group-id="${CSS.escape(groupWorkspaceState.selectedGroupId||"")}"]`)?.focus();});
  filteredNotice.append(document.createTextNode("The selected group is outside the current search. "),showGroup);
  const empty=document.createElement("p");empty.id="group-editor-empty";empty.className="group-editor-empty";empty.textContent="Select a group to view its membership and shared policies.";
  const loadStatus=document.createElement("div");loadStatus.id="group-policy-load-status";loadStatus.className="group-editor-empty hidden";loadStatus.setAttribute("role","status");loadStatus.setAttribute("aria-live","polite");
  const loadMessage=document.createElement("p");loadMessage.id="group-policy-load-message";loadStatus.append(loadMessage);
  const retryLoad=document.createElement("button");retryLoad.type="button";retryLoad.id="retry-group-policy-loads";retryLoad.textContent="Retry failed settings";retryLoad.className="hidden";retryLoad.addEventListener("click",()=>void retryGroupPolicyLoads());loadStatus.append(retryLoad);
  const editor=document.createElement("section");editor.id="group-management-editor";editor.className="group-general-editor";editor.setAttribute("aria-label","Selected group details");
  const editorPane=document.createElement("section");editorPane.className="group-editor-pane";editorPane.setAttribute("aria-labelledby","group-editor-heading");
  const editorHeading=document.createElement("h4");editorHeading.id="group-editor-heading";editorHeading.textContent="Select a group";
  const tabNav=document.createElement("nav");tabNav.id="group-policy-tabs";tabNav.className="group-editor-tabs";tabNav.setAttribute("role","tablist");tabNav.setAttribute("aria-label","Group policy sections");
  const panels=new Map();
  for(const tab of window.NvrCameraGroups.GROUP_POLICY_TABS){
    const panel=document.createElement("section");panel.id=`group-policy-panel-${tab.id}`;panel.className="group-policy-panel";panel.dataset.groupPolicyPanel=tab.id;panel.setAttribute("role","tabpanel");panel.setAttribute("aria-label",tab.label);panel.tabIndex=0;
    const actions=document.createElement("div");actions.className="group-tab-actions";
    const status=document.createElement("p");status.dataset.groupTabStatus=tab.id;status.setAttribute("role","status");status.setAttribute("aria-live","polite");status.textContent="Saved";status.dataset.state="saved";
    const save=document.createElement("button");save.type="button";save.textContent="Save";save.dataset.groupTabSave=tab.id;save.setAttribute("aria-label",`Save ${tab.label} settings`);save.addEventListener("click",()=>void saveGroupTab(tab.id));
    const revert=document.createElement("button");revert.type="button";revert.textContent="Revert";revert.dataset.groupTabRevert=tab.id;revert.setAttribute("aria-label",`Revert ${tab.label} settings`);revert.addEventListener("click",()=>void revertGroupTab(tab.id));
    actions.append(status,save,revert);panel.append(actions);panels.set(tab.id,panel);
    const button=document.createElement("button");button.type="button";button.id=`group-policy-tab-${tab.id}`;button.textContent=tab.label;button.dataset.groupPolicyTab=tab.id;button.setAttribute("role","tab");button.setAttribute("aria-controls",panel.id);
    panel.setAttribute("aria-labelledby",button.id);button.addEventListener("click",()=>void navigateGroupTab(tab.id));
    button.addEventListener("keydown",event=>{const buttons=[...tabNav.querySelectorAll('[role="tab"]')],index=buttons.indexOf(button);let next=null;if(event.key==="ArrowRight")next=buttons[(index+1)%buttons.length];else if(event.key==="ArrowLeft")next=buttons[(index-1+buttons.length)%buttons.length];else if(event.key==="Home")next=buttons[0];else if(event.key==="End")next=buttons.at(-1);if(next){event.preventDefault();next.focus();void navigateGroupTab(next.dataset.groupPolicyTab);}});
    tabNav.append(button);
  }
  const panelContent=document.createElement("div");panelContent.id="group-policy-panel-content";
  panels.get("general").append(editor);
  if(schedule)panels.get("recording").append(schedule);
  if(retention)panels.get("recording").append(retention);
  if(scheduleMessage)panels.get("recording").append(scheduleMessage);
  if(storage)panels.get("storage").append(storage);
  if(ai)panels.get("ai").append(ai);
  if(aiMessage)panels.get("ai").append(aiMessage);
  panelContent.append(...panels.values());
  editorPane.append(editorHeading,empty,loadStatus,filteredNotice,tabNav,panelContent);
  if(groupForm)listPane.append(groupForm);
  if(instructions){instructions.textContent="Browsing groups can overlap. A camera inherits recording, schedule, retention, storage, and AI settings from its single policy group unless a supported camera override applies.";listPane.append(instructions);}
  listPane.append(listHeading,searchLabel,search);
  if(list)listPane.append(list);
  listPane.append(createMessage);
  layout.append(listPane,editorPane);
  if(heading)root.replaceChildren(heading,layout);else root.replaceChildren(layout);
  if(runtime)document.querySelector("#app-settings-content")?.append(runtime);
  const scheduleSelect=document.querySelector("#schedule-group"),scheduleLabel=document.querySelector('label[for="schedule-group"]');
  if(scheduleSelect){scheduleSelect.hidden=true;scheduleSelect.setAttribute("aria-hidden","true");}
  if(scheduleLabel){scheduleLabel.hidden=true;scheduleLabel.setAttribute("aria-hidden","true");}
  for(const selector of ["#save-group-schedule","#save-group-retention","#save-storage-volume","#save-group-ai"]){const button=document.querySelector(selector);if(button)button.hidden=true;}
  search.addEventListener("input",()=>{groupWorkspaceState.setQuery(search.value);renderGroupManagement(groupWorkspaceState.groups);});
  groupForm.groupSaveConfig=async()=>{
    const name=document.querySelector("#new-group-name").value.trim();
    if(!name)throw new Error("Enter a camera group name.");
    if(name.length>80)throw new Error("Group names are limited to 80 characters.");
    const result=await api("/api/camera-groups",{method:"POST",body:JSON.stringify({name})});
    groupForm.reset();createMessage.textContent=`Created camera group ${result.name}.`;
  };
  if(schedule){schedule.groupSaveConfig=saveGroupSchedule;schedule.groupLoadConfig=loadGroupSchedule;}
  if(retention){retention.groupSaveConfig=saveGroupRetention;retention.groupLoadConfig=loadGroupRetention;}
  if(storage){storage.groupSaveConfig=saveGroupStorage;storage.groupLoadConfig=loadGroupStorage;}
  if(ai){ai.groupSaveConfig=saveGroupAi;ai.groupLoadConfig=loadGroupAi;}
  groupWorkspaceInitialized=true;
}
function setGroupTabStatus(tabId,text,state="saved"){
  const status=document.querySelector(`[data-group-tab-status="${tabId}"]`);if(!status)return;
  status.textContent=text;status.dataset.state=state;
}
function groupPolicyScopeIds(tabId){return GROUP_POLICY_SECTIONS[tabId]||[];}
function groupDirtyScopes(root=document.querySelector("#bulk-admin")){
  return [...(root?.matches?.('[data-config-dirty="true"]')?[root]:[]),...(root?.querySelectorAll?.('[data-config-dirty="true"]')||[])].filter(scope=>scope.dataset.configSection&&scope.closest("#bulk-admin"));
}
function groupTabDirtyScopes(tabId){
  const panel=document.querySelector(`[data-group-policy-panel="${tabId}"]`),ids=new Set(groupPolicyScopeIds(tabId));
  return [...(panel?.querySelectorAll?.('[data-config-dirty="true"]')||[])].filter(scope=>ids.has(scope.dataset.configSection));
}
function refreshGroupTabStatuses(){
  if(!groupWorkspaceInitialized)return;
  for(const tab of window.NvrCameraGroups.GROUP_POLICY_TABS){
    const dirty=groupTabDirtyScopes(tab.id),status=document.querySelector(`[data-group-tab-status="${tab.id}"]`);
    const save=document.querySelector(`[data-group-tab-save="${tab.id}"]`),revert=document.querySelector(`[data-group-tab-revert="${tab.id}"]`);
    if(save)save.disabled=!dirty.length||groupWorkspaceSaveInProgress||getRole()!=="administrator";
    if(revert)revert.disabled=!dirty.length||groupWorkspaceSaveInProgress||getRole()!=="administrator";
    if(!dirty.length)setGroupTabStatus(tab.id,"Saved","saved");
    else if(status?.dataset.state!=="saving"&&status?.dataset.state!=="error")setGroupTabStatus(tab.id,`${dirty.length} unsaved section${dirty.length===1?"":"s"}`,"dirty");
  }
}
function activateGroupTab(tabId){
  if(!groupWorkspaceState.selectedGroup()||!groupWorkspaceState.selectTab(tabId))return false;
  for(const button of document.querySelectorAll("#group-policy-tabs [role=tab]")){
    const active=button.dataset.groupPolicyTab===tabId;button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;
  }
  for(const panel of document.querySelectorAll("[data-group-policy-panel]"))panel.classList.toggle("hidden",panel.dataset.groupPolicyPanel!==tabId);
  refreshGroupTabStatuses();return true;
}
async function navigateGroupTab(tabId){
  if(tabId===groupWorkspaceState.activeTab)return true;
  if(!await guardGroupDrafts()){
    document.querySelector(`[role=tab][data-group-policy-tab="${CSS.escape(groupWorkspaceState.activeTab)}"]`)?.focus();return false;
  }
  return activateGroupTab(tabId);
}
function updateGroupEditorVisibility(){
  const selected=groupWorkspaceState.selectedGroup(),empty=document.querySelector("#group-editor-empty"),tabs=document.querySelector("#group-policy-tabs"),heading=document.querySelector("#group-editor-heading"),notice=document.querySelector("#group-filter-notice"),loadStatus=document.querySelector("#group-policy-load-status"),loadMessage=document.querySelector("#group-policy-load-message"),retry=document.querySelector("#retry-group-policy-loads");
  if(!empty||!tabs||!heading)return;
  const unavailable=Boolean(!selected&&unavailableGroupId&&groupDirtyScopes().length);
  const blockedByLoad=Boolean(selected&&(groupPolicyLoading||groupPolicyLoadFailures.size));
  empty.classList.toggle("hidden",Boolean(selected)||unavailable);tabs.classList.toggle("hidden",!selected||blockedByLoad);
  heading.textContent=selected?`${selected.name} · ${selected.camera_count} camera(s)`:unavailable?"Group unavailable":"Select a group";
  const visible=groupWorkspaceState.visibleGroups().some(group=>group.id===selected?.id);notice?.classList.toggle("hidden",!selected||visible);
  loadStatus?.classList.toggle("hidden",!blockedByLoad&&!unavailable);
  if(loadMessage)loadMessage.textContent=unavailable?"This group was removed while it had unsaved changes. Discard those changes before selecting another group.":selected?(groupPolicyLoading?`Loading shared settings for ${selected.name}…`:`Some settings for ${selected.name} could not be loaded. Retry before editing this group.`):"";
  retry?.classList.toggle("hidden",unavailable||groupPolicyLoading||!groupPolicyLoadFailures.size);
  for(const panel of document.querySelectorAll("[data-group-policy-panel]"))panel.classList.toggle("hidden",!selected||blockedByLoad||panel.dataset.groupPolicyPanel!==groupWorkspaceState.activeTab);
  if(selected&&!blockedByLoad)activateGroupTab(groupWorkspaceState.activeTab);
}
async function selectGroupForEditing(groupId){
  if(groupWorkspaceState.selectedGroupId===groupId)return true;
  if(!await guardGroupDrafts())return false;
  if(!groupWorkspaceState.selectGroup(groupId))return false;
  const select=document.querySelector("#schedule-group");if(select)select.value=groupId;
  groupPolicyLoadFailures.clear();resetGroupPolicyControls();groupPolicyLoading=true;
  renderGroupManagement(groupWorkspaceState.groups);
  const selection=groupWorkspaceState.captureSelection();
  await loadSelectedGroupPolicies(groupId,selection,["group-schedule","group-retention","group-storage","group-ai"]);
  return true;
}
function resetGroupPolicyControls(){
  document.querySelector("#group-schedule-timezone").value="UTC";renderScheduleEditor(document.querySelector("#group-schedule-editor"),[]);document.querySelector("#group-schedule-json").value="";
  document.querySelector("#group-retention-days").value="30";
  const storage=document.querySelector("#group-storage-volume");storage.replaceChildren(new Option("Loading storage volumes…","default"));document.querySelector("#storage-volume-status").replaceChildren();
  document.querySelector("#group-ai-enabled").checked=false;document.querySelector("#group-ai-fps").value="0.5";document.querySelector("#group-ai-confidence").value="0.55";document.querySelector("#group-ai-substream").checked=true;document.querySelector("#group-ai-zones").value="[]";renderZoneEditor(document.querySelector("#group-ai-zones-editor"),[]);
  document.querySelector("#schedule-message").textContent="";document.querySelector("#ai-group-message").textContent="";
}
async function loadSelectedGroupPolicies(groupId,selection,sectionIds){
  groupPolicyLoading=true;updateGroupEditorVisibility();
  const scopes=sectionIds.map(id=>document.querySelector(`#bulk-admin [data-config-section="${id}"]`)).filter(Boolean);
  const results=await Promise.all(scopes.map(async scope=>{
    try{const loaded=await scope.groupLoadConfig?.(groupId,selection);return {id:scope.dataset.configSection,loaded:loaded!==false};}
    catch(error){setGroupTabStatus(GROUP_SECTION_TAB[scope.dataset.configSection],`Load failed: ${error.message}`,"error");return {id:scope.dataset.configSection,error};}
  }));
  if(!groupWorkspaceState.isCurrent(selection))return false;
  for(const result of results){if(result.error||!result.loaded)groupPolicyLoadFailures.add(result.id);else groupPolicyLoadFailures.delete(result.id);}
  groupPolicyLoading=false;updateGroupEditorVisibility();return groupPolicyLoadFailures.size===0;
}
async function retryGroupPolicyLoads(){
  const selected=groupWorkspaceState.selectedGroup();if(!selected||groupPolicyLoading||!groupPolicyLoadFailures.size)return false;
  const failed=[...groupPolicyLoadFailures],selection=groupWorkspaceState.captureSelection();
  groupPolicyLoadFailures.clear();
  return loadSelectedGroupPolicies(selected.id,selection,failed);
}
function renderGroupManagement(groups){
  const target=document.querySelector("#group-management-list"),editor=document.querySelector("#group-management-editor");
  if(!target||!editor)return;
  const visible=groupWorkspaceState.visibleGroups().slice().sort((a,b)=>a.name.localeCompare(b.name)||a.id.localeCompare(b.id));
  target.replaceChildren(...visible.map(group=>{
    const button=document.createElement("button");button.type="button";button.dataset.groupId=group.id;button.setAttribute("aria-pressed",String(group.id===groupWorkspaceState.selectedGroupId));
    const name=document.createElement("span");name.textContent=group.name;
    const count=document.createElement("span");count.className="group-camera-count";count.textContent=`${group.camera_count} camera${group.camera_count===1?"":"s"}`;
    button.append(name,count);button.addEventListener("click",()=>void selectGroupForEditing(group.id));return button;
  }));
  if(!visible.length){const empty=document.createElement("p");empty.className="muted";empty.textContent=groups.length?"No groups match this search.":"No camera groups yet. Create a group to manage shared policies.";target.append(empty);}
  for(const button of target.querySelectorAll("[data-group-id]"))button.setAttribute("aria-pressed",String(button.dataset.groupId===groupWorkspaceState.selectedGroupId));
  const selected=groupWorkspaceState.selectedGroup();
  if(!selected){editor.replaceChildren();updateGroupEditorVisibility();return;}
  const dirty=editor.querySelector('[data-config-dirty="true"]');
  if(!dirty||editor.dataset.groupId!==selected.id){
    editor.dataset.groupId=selected.id;editor.replaceChildren();
    const namePanel=document.createElement("fieldset"),nameLegend=document.createElement("legend");namePanel.dataset.configSection="group-name";nameLegend.textContent="Group name";namePanel.append(nameLegend);
    const nameLabel=document.createElement("label"),name=document.createElement("input");name.type="text";name.value=selected.name;name.maxLength=80;name.autocomplete="off";nameLabel.textContent="Policy group name ";nameLabel.append(name);namePanel.append(nameLabel);namePanel.groupSaveConfig=async()=>{const value=name.value.trim();if(!value)throw new Error("Group names cannot be empty.");if(value.length>80)throw new Error("Group names are limited to 80 characters.");await api(`/api/camera-groups/${encodeURIComponent(selected.id)}`,{method:"PATCH",body:JSON.stringify({name:value})});};
    const members=new Set(selected.camera_ids||[]),memberPanel=document.createElement("fieldset"),memberLegend=document.createElement("legend");memberPanel.dataset.configSection="group-members";memberLegend.textContent="Browsing membership";memberPanel.append(memberLegend);
    const help=document.createElement("p");help.textContent="Browsing membership can overlap. A camera’s single policy group controls inherited settings.";memberPanel.append(help);
    for(const camera of [...getCameras()].sort((a,b)=>a.name.localeCompare(b.name)||a.id.localeCompare(b.id))){
      const label=document.createElement("label"),checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.value=camera.id;checkbox.checked=members.has(camera.id);
      if(camera.group_id===selected.id){checkbox.disabled=true;checkbox.title="A camera remains a member of its policy group automatically.";}
      label.append(checkbox,document.createTextNode(` ${camera.name}${camera.group_id===selected.id?" · policy group":""}`));memberPanel.append(label);
    }
    memberPanel.groupSaveConfig=async()=>{const camera_ids=[...memberPanel.querySelectorAll('input[type="checkbox"]:checked')].map(input=>input.value);await api(`/api/camera-groups/${encodeURIComponent(selected.id)}/members`,{method:"PUT",body:JSON.stringify({camera_ids})});};
    const remove=document.createElement("button");remove.type="button";remove.textContent="Delete group";remove.addEventListener("click",async()=>{if(!window.confirm(`Delete group “${selected.name}”? A group assigned to camera policies must first be reassigned.`))return;if(!await guardGroupDrafts())return;try{await api(`/api/camera-groups/${encodeURIComponent(selected.id)}`,{method:"DELETE"});await refreshGroups();}catch(error){document.querySelector("#group-create-message").textContent=error.message;}});
    editor.append(namePanel,memberPanel,remove);
  }
  updateGroupEditorVisibility();
}
async function loadGroupSchedule(groupId,selection=groupWorkspaceState.captureSelection()){
  const result=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/schedule`);if(!groupWorkspaceState.isCurrent(selection))return false;
  const timezone=document.querySelector("#group-schedule-timezone"),editor=document.querySelector("#group-schedule-editor"),advanced=document.querySelector("#group-schedule-json");
  timezone.value=result.timezone||"UTC";renderScheduleEditor(editor,result.intervals||[]);advanced.value=JSON.stringify({timezone:result.timezone,intervals:result.intervals},null,2);
  document.querySelector("#schedule-message").textContent=result.configured?"Group schedule loaded.":"No group schedule is configured.";return true;
}
async function saveGroupSchedule(groupId){
  const schedule={timezone:document.querySelector("#group-schedule-timezone").value.trim()||"UTC",intervals:readScheduleEditor(document.querySelector("#group-schedule-editor"))};
  const result=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/schedule`,{method:"PUT",body:JSON.stringify(schedule)});
  document.querySelector("#group-schedule-json").value=JSON.stringify(schedule,null,2);document.querySelector("#schedule-message").textContent=`Saved ${result.intervals.length} group schedule interval(s).`;
}
async function loadGroupRetention(groupId,selection=groupWorkspaceState.captureSelection()){
  const policy=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/retention`);if(!groupWorkspaceState.isCurrent(selection))return false;
  document.querySelector("#group-retention-days").value=policy.retention_days;document.querySelector("#schedule-message").textContent=policy.configured?"Group retention loaded.":"Group uses the 30 day default.";return true;
}
async function saveGroupRetention(groupId){
  const retention_days=Number(document.querySelector("#group-retention-days").value),policy=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/retention`,{method:"PUT",body:JSON.stringify({retention_days})});
  document.querySelector("#schedule-message").textContent=`Saved group retention target: ${policy.retention_days} days.`;
}
async function loadGroupStorage(groupId,selection=groupWorkspaceState.captureSelection()){
  const [status,assignment]=await Promise.all([api("/api/storage"),api(`/api/camera-groups/${encodeURIComponent(groupId)}/storage-volume`)]);if(!groupWorkspaceState.isCurrent(selection))return false;
  const select=document.querySelector("#group-storage-volume");select.replaceChildren(...status.volumes.map(volume=>new Option(`${volume.id} · ${volume.state} · ${volume.free_bytes==null?"capacity unknown":Math.round(volume.free_bytes/1048576)+" MiB free"}`,volume.id)));select.value=assignment.volume_id;
  document.querySelector("#storage-volume-status").replaceChildren(...status.volumes.map(volume=>{const item=document.createElement("li");item.textContent=`${volume.id}: ${volume.state}; ${volume.cameras.length} assigned camera(s)${volume.retention_at_risk?" · retention at risk":""}`;return item;}));return true;
}
async function saveGroupStorage(groupId){
  const volume_id=document.querySelector("#group-storage-volume").value,result=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/storage-volume`,{method:"PUT",body:JSON.stringify({volume_id})});
  document.querySelector("#schedule-message").textContent=`Assigned group cameras to ${result.volume_id}.`;
}
async function loadGroupAi(groupId,selection=groupWorkspaceState.captureSelection()){
  const config=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/ai`);if(!groupWorkspaceState.isCurrent(selection))return false;
  document.querySelector("#group-ai-enabled").checked=config.enabled;document.querySelector("#group-ai-fps").value=config.analysis_fps;document.querySelector("#group-ai-confidence").value=config.confidence_threshold;
  document.querySelector("#group-ai-substream").checked=config.use_substream;document.querySelector("#group-ai-zones").value=JSON.stringify(config.zones);
  renderZoneEditor(document.querySelector("#group-ai-zones-editor"),config.zones||[]);
  document.querySelector("#ai-group-message").textContent=`${config.group_name}: ${config.inherited_camera_count}/${config.camera_count} camera(s) inherit these settings · ${config.model.available?"model ready":config.model.reason}.`;
  return true;
}
async function saveGroupAi(groupId){
  const zones=readZoneEditor(document.querySelector("#group-ai-zones-editor"));document.querySelector("#group-ai-zones").value=JSON.stringify(zones);
  const result=await api(`/api/camera-groups/${encodeURIComponent(groupId)}/ai`,{method:"PUT",body:JSON.stringify({enabled:document.querySelector("#group-ai-enabled").checked,analysis_fps:Number(document.querySelector("#group-ai-fps").value),confidence_threshold:Number(document.querySelector("#group-ai-confidence").value),use_substream:document.querySelector("#group-ai-substream").checked,zones})});
  document.querySelector("#ai-group-message").textContent=`Saved group settings for ${result.inherited_camera_count} inherited camera(s). Cameras beyond the configured AI limit will show skipped analysis coverage.`;
}
function groupScopeLabel(scope){return scope?.querySelector(":scope > legend")?.textContent?.trim()||scope?.dataset.configSection||"Group settings";}
function lockGroupControls(){
  const controls=new Map(),root=document.querySelector("#bulk-admin");
  for(const control of root.querySelectorAll("input,select,textarea,button")){controls.set(control,control.disabled);control.disabled=true;}
  return ()=>{for(const [control,disabled] of controls)control.disabled=disabled;};
}
async function saveGroupScopes(scopes,{refreshAfter=false,statusTabId=null}={}){
  if(groupWorkspaceSaveInProgress)return false;
  const unique=[...new Set(scopes)].filter(Boolean);if(!unique.length)return true;
  const groupId=groupWorkspaceState.selectedGroupId,selection=groupWorkspaceState.captureSelection();
  if(unique.some(scope=>scope.dataset.configSection!=="group-form")&&!groupId)return false;
  const affectedTabs=new Set(unique.map(scope=>GROUP_SECTION_TAB[scope.dataset.configSection]).filter(Boolean));if(statusTabId)affectedTabs.add(statusTabId);
  const operations=unique.map(scope=>({
    scope,
    label:groupScopeLabel(scope),
    unavailableMessage:`Saving ${groupScopeLabel(scope)} is not available.`,
    staleMessage:"Group selection changed during save. Reload the selected group before retrying.",
    save:()=>{
      if(typeof scope.groupSaveConfig!=="function")throw new Error(`Saving ${groupScopeLabel(scope)} is not available.`);
      return scope.groupSaveConfig(groupId);
    },
    isCurrent:()=>scope.dataset.configSection==="group-form"||groupWorkspaceState.isCurrent(selection),
    clear:()=>clearConfigDraft(scope),
  }));
  const outcome=await window.NvrCameraAdminSave.runScopedSave(operations,{
    lock:()=>lockGroupControls(),
    onStart:()=>{groupWorkspaceSaveInProgress=true;affectedTabs.forEach(tab=>setGroupTabStatus(tab,"Saving…","saving"));refreshGroupTabStatuses();},
    onFinish:()=>{groupWorkspaceSaveInProgress=false;refreshGroupTabStatuses();},
  });
  if(!outcome.complete){
    const failedScope=outcome.failedOperation?.scope,saveError=outcome.error;
    const failedTab=GROUP_SECTION_TAB[failedScope?.dataset.configSection]||statusTabId;
    if(failedTab)setGroupTabStatus(failedTab,`Save failed: ${saveError.message}`,"error");
    const status=failedScope?.dataset.configSection==="group-form"?document.querySelector("#group-create-message"):failedTab==="ai"?document.querySelector("#ai-group-message"):document.querySelector("#schedule-message");
    if(status)status.textContent=outcome.savedLabels.length?`${outcome.savedLabels.join(", ")} saved. ${groupScopeLabel(failedScope)} failed: ${saveError.message} Remaining changes are still available to save.`:`${groupScopeLabel(failedScope)} could not be saved: ${saveError.message} Your changes are still available.`;
    refreshGroupTabStatuses();return false;
  }
  for(const tab of affectedTabs)setGroupTabStatus(tab,"Saved","saved");refreshGroupTabStatuses();
  if(refreshAfter){try{await refreshApp();}catch(error){const status=statusTabId==="ai"?document.querySelector("#ai-group-message"):document.querySelector("#schedule-message");if(status)status.textContent=`Settings saved, but the workspace could not refresh: ${error.message}`;}}
  return true;
}
async function revertGroupScopes(scopes){
  if(groupWorkspaceSaveInProgress)return false;
  const unique=[...new Set(scopes)].filter(Boolean),groupId=groupWorkspaceState.selectedGroupId,selection=groupWorkspaceState.captureSelection();
  if(unique.some(scope=>scope.dataset.configSection!=="group-form")&&!groupId)return false;
  const unlock=lockGroupControls();groupWorkspaceSaveInProgress=true;refreshGroupTabStatuses();let failure=null,needsGroupRefresh=false;
  try{
    for(const scope of unique){
      if(scope.dataset.configSection==="group-form"){scope.reset();clearConfigDraft(scope);continue;}
      if(["group-name","group-members"].includes(scope.dataset.configSection)){needsGroupRefresh=true;continue;}
      if(typeof scope.groupLoadConfig!=="function")throw new Error(`Reloading ${groupScopeLabel(scope)} is not available.`);
      await scope.groupLoadConfig(groupId,selection);
      if(!groupWorkspaceState.isCurrent(selection))throw new Error("The selected group changed while saved settings were loading.");
      clearConfigDraft(scope);
    }
    if(needsGroupRefresh){
      const {groups}=await api("/api/camera-groups");if(!groupWorkspaceState.isCurrent(selection))throw new Error("The selected group changed while saved settings were loading.");
      groupWorkspaceState.setGroups(groups);setSidebarGroups(groups);clearConfigDrafts(unique.filter(scope=>["group-name","group-members"].includes(scope.dataset.configSection)));renderGroupManagement(groups);
    }
  }catch(error){failure=error;}
  finally{unlock();groupWorkspaceSaveInProgress=false;refreshGroupTabStatuses();}
  if(failure){const tab=GROUP_SECTION_TAB[unique.find(scope=>scope.dataset.configDirty==="true")?.dataset.configSection]||groupWorkspaceState.activeTab;setGroupTabStatus(tab,`Revert failed: ${failure.message}`,"error");return false;}
  return true;
}
function clearConfigDrafts(scopes){scopes.forEach(clearConfigDraft);}
async function saveGroupTab(tabId=groupWorkspaceState.activeTab){
  const dirty=groupTabDirtyScopes(tabId);if(!dirty.length)return true;return saveGroupScopes(dirty,{refreshAfter:true,statusTabId:tabId});
}
async function revertGroupTab(tabId=groupWorkspaceState.activeTab){
  const dirty=groupTabDirtyScopes(tabId);if(!dirty.length)return true;
  const success=await revertGroupScopes(dirty);if(success)setGroupTabStatus(tabId,"Reverted to saved settings","saved");return success;
}
async function guardGroupDrafts(){
  if(groupWorkspaceSaveInProgress)return false;
  const dirty=groupDirtyScopes();if(!dirty.length)return true;
  const decision=await askCameraDirtyDecision({title:"Unsaved group settings",description:"Save or discard group changes before continuing."});
  if(decision==="save"){
    if(unavailableGroupId&&!groupWorkspaceState.selectedGroup()){
      updateGroupEditorVisibility();return false;
    }
    return saveGroupScopes(dirty,{refreshAfter:true,statusTabId:groupWorkspaceState.activeTab});
  }
  if(decision==="discard"){
    if(unavailableGroupId&&!groupWorkspaceState.selectedGroup()){
      clearConfigDrafts(dirty);groupWorkspaceState.clearSelection();unavailableGroupId=null;groupPolicyLoadFailures.clear();groupPolicyLoading=false;resetGroupPolicyControls();renderGroupManagement(groupWorkspaceState.groups);return true;
    }
    return revertGroupScopes(dirty);
  }
  return false;
}
async function loadGroupScopeWithGuard(sectionId){
  if(!await guardGroupDrafts())return false;
  const scope=document.querySelector(`#bulk-admin [data-config-section="${sectionId}"]`),groupId=groupWorkspaceState.selectedGroupId,selection=groupWorkspaceState.captureSelection();
  if(!scope||!groupId)return false;
  const tab=GROUP_SECTION_TAB[sectionId],unlock=lockGroupControls();if(tab)setGroupTabStatus(tab,"Loading saved settings…","saving");
  try{
    const loaded=await scope.groupLoadConfig?.(groupId,selection);if(!loaded||!groupWorkspaceState.isCurrent(selection))return false;clearConfigDraft(scope);
    if(sectionId==="group-ai"&&groupWorkspaceState.isCurrent(selection)){const representative=groupWorkspaceState.selectedGroup()?.camera_ids?.[0];if(representative)await loadRepresentativeZoneFrame(representative,document.querySelector("#group-ai-zones-editor"),()=>groupWorkspaceState.isCurrent(selection));}
    if(tab)setGroupTabStatus(tab,"Saved settings loaded","saved");
    return true;
  }
  catch(error){const message=sectionId==="group-ai"?document.querySelector("#ai-group-message"):document.querySelector("#schedule-message");if(message)message.textContent=error.message;if(tab)setGroupTabStatus(tab,`Load failed: ${error.message}`,"error");return false;}
  finally{unlock();}
}
async function refreshGroups(){
  setupGroupPolicyWorkspace();
  const {groups}=await api("/api/camera-groups");
  const priorSelection=groupWorkspaceState.selectedGroupId,hadSelectedGroup=Boolean(priorSelection);
  groupWorkspaceState.setGroups(groups);setSidebarGroups(groups);
  if(hadSelectedGroup&&!groupWorkspaceState.selectedGroupId&&groupDirtyScopes().length)unavailableGroupId=priorSelection;
  if(unavailableGroupId&&groups.some(group=>group.id===unavailableGroupId)){groupWorkspaceState.selectGroup(unavailableGroupId);unavailableGroupId=null;}
  const select=document.querySelector("#bulk-group");
  const options=[new Option("Leave groups unchanged","__unchanged__"),new Option("Remove from group","")];
  for(const group of groups)options.push(new Option(group.name,group.name));
  select?.replaceChildren(...options);
  const scheduleSelect=document.querySelector("#schedule-group");
  if(scheduleSelect){scheduleSelect.replaceChildren(new Option("No group selected",""),...groups.map(group=>new Option(group.name,group.id)));scheduleSelect.value=groupWorkspaceState.selectedGroupId||"";}
  const editor=document.querySelector("#group-management-editor");
  if(!editor?.querySelector('[data-config-dirty="true"]'))renderGroupManagement(groups);
  else if(unavailableGroupId)updateGroupEditorVisibility();
  const groupButton=document.querySelector('[data-camera-workspace-button="groups"]');groupButton?.classList.toggle("hidden",getRole()!=="administrator");
  try{const runtime=await api("/api/ai/settings");const max=document.querySelector("#ai-max-active");if(max)max.value=runtime.max_active_cameras;}catch{}
}
root.querySelector("#group-form").addEventListener("submit",async event=>{
  event.preventDefault();await saveGroupScopes([event.currentTarget],{refreshAfter:true});
});
root.querySelector("#load-group-schedule").addEventListener("click",()=>void loadGroupScopeWithGuard("group-schedule"));
root.querySelector("#save-group-schedule").addEventListener("click",()=>void saveGroupTab("recording"));
root.querySelector("#clear-group-schedule").addEventListener("click",async()=>{
  if(!await guardGroupDrafts())return;const group=groupWorkspaceState.selectedGroupId;
  if(!group){document.querySelector("#schedule-message").textContent="Select a group first.";return;}
  const selection=groupWorkspaceState.captureSelection(),unlock=lockGroupControls();let removed=false;
  try{await api(`/api/camera-groups/${encodeURIComponent(group)}/schedule`,{method:"DELETE"});removed=true;const scope=document.querySelector('#group-policy-panel-recording [data-config-section="group-schedule"]');clearConfigDraft(scope);await loadGroupSchedule(group,selection);if(groupWorkspaceState.isCurrent(selection))document.querySelector("#schedule-message").textContent="Group schedule removed; assigned cameras use their camera policy or camera-level override.";}
  catch(error){if(groupWorkspaceState.isCurrent(selection))document.querySelector("#schedule-message").textContent=error.message;}
  finally{unlock();}
if(removed)await refreshApp();
});
root.querySelector("#add-group-schedule-interval").addEventListener("click",()=>{const editor=root.querySelector("#group-schedule-editor");markConfigDraftElement(editor);addScheduleInterval(editor);});
root.querySelector("#load-group-retention").addEventListener("click",()=>void loadGroupScopeWithGuard("group-retention"));
root.querySelector("#save-group-retention").addEventListener("click",()=>void saveGroupTab("recording"));
root.querySelector("#clear-group-retention").addEventListener("click",async()=>{
  if(!await guardGroupDrafts())return;const group=groupWorkspaceState.selectedGroupId;
  if(!group){document.querySelector("#schedule-message").textContent="Select a group first.";return;}
  const selection=groupWorkspaceState.captureSelection(),unlock=lockGroupControls();let removed=false;
  try{await api(`/api/camera-groups/${encodeURIComponent(group)}/retention`,{method:"DELETE"});removed=true;const scope=document.querySelector('#group-policy-panel-recording [data-config-section="group-retention"]');clearConfigDraft(scope);await loadGroupRetention(group,selection);if(groupWorkspaceState.isCurrent(selection))document.querySelector("#schedule-message").textContent="Group retention removed; cameras use their camera override or the 30 day default.";}
  catch(error){if(groupWorkspaceState.isCurrent(selection))document.querySelector("#schedule-message").textContent=error.message;}
  finally{unlock();}
  if(removed)await refreshApp();
});
root.querySelector("#load-storage-volumes").addEventListener("click",()=>void loadGroupScopeWithGuard("group-storage"));
root.querySelector("#save-storage-volume").addEventListener("click",()=>void saveGroupTab("storage"));

    root.querySelector("#load-group-ai")?.addEventListener("click",()=>void loadGroupScopeWithGuard("group-ai"));
    root.querySelector("#save-group-ai")?.addEventListener("click",()=>void saveGroupTab("ai"));
    setupGroupPolicyWorkspace();
    return {
      state: groupWorkspaceState,
      refresh: refreshGroups,
      render: renderGroupManagement,
      guardDrafts: guardGroupDrafts,
      resetSession() {
        groupWorkspaceState.clearSelection();
        groupWorkspaceState.setQuery("");
        groupPolicyLoading=false;
        groupPolicyLoadFailures.clear();
        unavailableGroupId=null;
        const search=root.querySelector("#group-search");
        if(search)search.value="";
        updateGroupEditorVisibility();
      },
    };
  }

  return { GROUP_POLICY_TABS, createGroupWorkspaceState, mountWorkspace };
});
