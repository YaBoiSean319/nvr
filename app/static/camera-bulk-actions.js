(function installCameraBulkActions(root, createModule) {
  const bulkActions = createModule();
  if (typeof module !== "undefined" && module.exports) module.exports = bulkActions;
  root.NvrCameraBulkActions = bulkActions;
})(typeof window === "undefined" ? globalThis : window, function createCameraBulkActions() {
  function uniqueIds(values) {
    return [...new Set([...values].map(value => String(value)))];
  }

  function selectionCounts(selectedIds, cameras, visibleIds) {
    const selected = new Set(uniqueIds(selectedIds));
    const cameraIds = new Set((cameras || []).map(camera => String(camera.id)));
    const visible = new Set((visibleIds || []).map(value => String(value)));
    let hidden = 0;
    let unavailable = 0;
    for (const id of selected) {
      if (!cameraIds.has(id)) unavailable += 1;
      else if (!visible.has(id)) hidden += 1;
    }
    return { selected: selected.size, hidden, unavailable };
  }

  function createReview(selectedIds, cameras, visibleIds, changes) {
    const cameraById = new Map((cameras || []).map(camera => [String(camera.id), camera]));
    const cameraIds = uniqueIds(selectedIds);
    const targets = cameraIds.map(id => {
      const camera = cameraById.get(id);
      if (!camera) return { id, name: "Camera no longer available", status: "missing" };
      if (camera.archived_at) return { id, name: camera.name || id, status: "archived" };
      return {
        id,
        name: camera.name || id,
        status: "ready",
        current: {
          groupName: camera.group_name || null,
          recordingMode: camera.recording_mode || null,
          effectiveRecordingMode: camera.effective_recording_mode || camera.recording_mode || null,
          recordingEnabled: Boolean(camera.recording_enabled),
        },
      };
    });
    return {
      cameraIds,
      targets,
      changes: { ...changes },
      counts: selectionCounts(cameraIds, cameras, visibleIds),
    };
  }

  function orderedResults(targetIds, results) {
    const byId = new Map((results || []).map(result => [String(result.camera_id), result]));
    return uniqueIds(targetIds).map(id => byId.get(id) || {
      camera_id: id,
      status: "unknown",
      code: "missing_result",
      detail: "The server did not report an outcome for this camera.",
    });
  }

  function changesMatch(camera, changes) {
    if (Object.prototype.hasOwnProperty.call(changes, "group_name") && (camera.group_name || null) !== (changes.group_name || null)) return false;
    if (changes.recording_mode && camera.recording_mode !== changes.recording_mode) return false;
    if (Object.prototype.hasOwnProperty.call(changes, "recording_enabled")) {
      const resultingMode = changes.recording_mode || camera.recording_mode;
      const expected = Boolean(changes.recording_enabled && resultingMode !== "disabled");
      if (Boolean(camera.recording_enabled) !== expected) return false;
    }
    if (changes.recording_mode === "disabled" && camera.recording_enabled) return false;
    return true;
  }

  function plannedState(target, changes = {}) {
    const current = target.current || {};
    const mode = changes.recording_mode || current.effectiveRecordingMode || current.recordingMode || "Not reported";
    const resultingMode = changes.recording_mode || current.recordingMode;
    let recordingEnabled = Boolean(current.recordingEnabled);
    if (Object.prototype.hasOwnProperty.call(changes, "recording_enabled")) {
      recordingEnabled = Boolean(changes.recording_enabled && resultingMode !== "disabled");
    }
    if (resultingMode === "disabled") recordingEnabled = false;
    return {
      groupName: Object.prototype.hasOwnProperty.call(changes, "group_name") ? changes.group_name || null : current.groupName || null,
      recordingMode: mode,
      recordingEnabled,
      enablementBlockedByMode: changes.recording_enabled === true && resultingMode === "disabled",
    };
  }

  function reconcileResults(targetIds, results, cameras, changes) {
    if (!Array.isArray(cameras)) return orderedResults(targetIds, results);
    const cameraById = new Map((cameras || []).map(camera => [String(camera.id), camera]));
    return orderedResults(targetIds, results).map(result => {
      if (result.status !== "unknown" && result.status !== "failed") return { ...result };
      const camera = cameraById.get(String(result.camera_id));
      if (!camera) return { ...result, status: "failed", code: "missing", detail: "Camera is no longer available; it was not retried." };
      if (camera.archived_at) return { ...result, status: "failed", code: "archived", detail: "Camera is archived; it was not retried." };
      if (changesMatch(camera, changes)) return { ...result, status: "success", code: "reconciled", detail: "The requested settings are visible in the inventory." };
      if (result.status === "failed" && !["missing", "archived", "unavailable", "not_applied"].includes(result.code)) return { ...result };
      return { ...result, status: "failed", code: "not_applied", detail: "The requested settings are not visible in the inventory and can be retried." };
    });
  }

  function retryableIds(results, cameras) {
    const activeIds = new Set((cameras || []).filter(camera => !camera.archived_at).map(camera => String(camera.id)));
    return (results || [])
      .filter(result => result.status === "failed" && result.code !== "unauthorized" && result.code !== "permission_denied" && activeIds.has(String(result.camera_id)))
      .map(result => String(result.camera_id));
  }

  function mountWorkspace(root, services = {}) {
    if (!root) return null;
    const {
      api, getCameras = () => [], setCameras = () => {}, getRole = () => null,
      renderCameras = () => {}, renderSidebar = () => {}, clearConfigDraft = () => {},
    } = services;
    const cameraBulkSelection = new Set();
    let cameraBulkVisibleIds = [];
    let cameraBulkReview = null;
    let cameraBulkResults = [];
    let cameraBulkOperationInProgress = false;
    let cameraBulkSession = 0;
    let cameraBulkStatusDialog = null;
function refreshCameraBulkToolbar(){
  const toolbar=document.querySelector("#camera-bulk-toolbar"),summary=document.querySelector("#camera-bulk-summary"),bulkUpdate=document.querySelector('[data-config-section="bulk-update"]');
  if(!toolbar||!summary)return;
  const admin=getRole()==="administrator";
  const counts=window.NvrCameraBulkActions.selectionCounts(cameraBulkSelection,getCameras(),cameraBulkVisibleIds);
  const hasSelection=admin&&counts.selected>0;
  toolbar.hidden=!hasSelection;
  if(hasSelection)summary.textContent=`${counts.selected} selected · ${counts.hidden} hidden by filters${counts.unavailable?` · ${counts.unavailable} no longer in inventory`:""}`;
  const selectMatching=document.querySelector("#camera-bulk-select-matching"),clear=document.querySelector("#camera-bulk-clear-selection");
  const allVisibleSelected=cameraBulkVisibleIds.length>0&&cameraBulkVisibleIds.every(id=>cameraBulkSelection.has(id));
  if(selectMatching){selectMatching.textContent=`Select all ${cameraBulkVisibleIds.length} matching cameras`;selectMatching.disabled=!admin||cameraBulkOperationInProgress||!cameraBulkVisibleIds.length||allVisibleSelected;}
  if(clear)clear.disabled=cameraBulkOperationInProgress||counts.selected===0;
  if(bulkUpdate){bulkUpdate.hidden=!hasSelection;bulkUpdate.disabled=cameraBulkOperationInProgress;const button=bulkUpdate.querySelector("#apply-bulk");if(button)button.disabled=!hasSelection||cameraBulkOperationInProgress;}
  for(const selector of ["#bulk-group","#bulk-recording-mode","#bulk-recording-enabled"]){const input=document.querySelector(selector);if(input)input.disabled=cameraBulkOperationInProgress;}

  const status=document.querySelector("#camera-bulk-operation-status"),statusText=document.querySelector("#camera-bulk-operation-summary");
  if(!status||!statusText)return;
  const hasOperation=Boolean(cameraBulkReview&&(cameraBulkOperationInProgress||cameraBulkResults.length));
  status.hidden=!hasOperation;
  if(!hasOperation)return;
  const totals=cameraBulkResults.reduce((counts,result)=>{counts[result.status]=(counts[result.status]||0)+1;return counts;},{});
  const success=totals.success||0,failed=totals.failed||0,unknown=totals.unknown||0,pending=totals.pending||0;
  if(cameraBulkOperationInProgress)statusText.textContent=`Bulk update running for ${cameraBulkReview.cameraIds.length} reviewed camera(s); ${pending} outcome(s) are waiting. Closing this panel will not cancel the update.`;
  else statusText.textContent=`Bulk update finished: ${success} succeeded, ${failed} failed, ${unknown} need verification.`;
}
function resetCameraBulkSession(){
  cameraBulkSession+=1;cameraBulkSelection.clear();cameraBulkVisibleIds=[];cameraBulkReview=null;cameraBulkResults=[];cameraBulkOperationInProgress=false;
  if(cameraBulkStatusDialog?.open)cameraBulkStatusDialog.close();
  cameraBulkStatusDialog=null;refreshCameraBulkToolbar();
}
function bulkActionChanges(){
  const changes={};
  const group=document.querySelector("#bulk-group").value;
  const mode=document.querySelector("#bulk-recording-mode").value;
  const recordingEnabled=document.querySelector("#bulk-recording-enabled").value;
  if(group!=="__unchanged__")changes.group_name=group||null;
  if(mode)changes.recording_mode=mode;
  if(recordingEnabled)changes.recording_enabled=recordingEnabled==="true";
  return changes;
}
function bulkChangeDescriptions(changes){
  const descriptions=[];
  if(Object.prototype.hasOwnProperty.call(changes,"group_name"))descriptions.push(changes.group_name?`Assign policy group “${changes.group_name}”.`:"Remove the policy group.");
  if(changes.recording_mode)descriptions.push(`Set recording mode to ${changes.recording_mode}.`);
  if(Object.prototype.hasOwnProperty.call(changes,"recording_enabled"))descriptions.push(`${changes.recording_enabled?"Enable":"Disable"} recording.`);
  return descriptions;
}
function bulkTargetStateText(target,changes,after=false){
  if(target.status!=="ready")return after?"Excluded; no settings will be changed during this apply.":target.status==="archived"?"Archived camera":"Camera no longer available";
  const state=window.NvrCameraBulkActions.plannedState(target,after?changes:{});
  const result=[`Policy group: ${state.groupName||"None"}`,`Recording mode: ${state.recordingMode}`,`Recording: ${state.recordingEnabled?"enabled":"disabled"}`];
  if(after&&state.enablementBlockedByMode)result.push("mode keeps recording disabled");
  return result.join(" · ");
}
function openBulkReviewDialog(){
  if(cameraBulkOperationInProgress)return;
  if(cameraBulkReview&&cameraBulkResults.some(result=>["failed","unknown"].includes(result.status))){
    document.querySelector("#bulk-message").textContent="Resolve or explicitly dismiss the previous per-camera outcomes before starting another bulk change.";
    openBulkResultsDialog();return;
  }
  let review;
  try{
    if(!cameraBulkSelection.size)throw new Error("Select at least one camera using its checkbox.");
    const changes=bulkActionChanges();
    if(!Object.keys(changes).length)throw new Error("Choose at least one change to review.");
    review=window.NvrCameraBulkActions.createReview(cameraBulkSelection,getCameras(),cameraBulkVisibleIds,changes);
  }catch(error){document.querySelector("#bulk-message").textContent=error.message;return;}

  const dialog=document.createElement("dialog");dialog.className="camera-bulk-dialog";dialog.setAttribute("aria-labelledby","camera-bulk-review-title");
  const title=document.createElement("h2");title.id="camera-bulk-review-title";title.textContent="Review camera changes";title.tabIndex=-1;
  const intro=document.createElement("p");intro.textContent=`This review is fixed to ${review.cameraIds.length} selected camera ID(s). Filtering or refreshing the table will not change this target list.`;
  const changeHeading=document.createElement("h3");changeHeading.textContent="Changes to apply";
  const changesList=document.createElement("ul");changesList.replaceChildren(...bulkChangeDescriptions(review.changes).map(description=>{const item=document.createElement("li");item.textContent=description;return item;}));
  let policyNote=null;
  if(Object.prototype.hasOwnProperty.call(review.changes,"group_name")){
    policyNote=document.createElement("p");policyNote.textContent="Changing the policy group may change inherited schedule, retention, storage, and AI settings for cameras without their own overrides. Browsing-group membership and camera-level overrides are unchanged.";
  }
  const eligible=review.targets.filter(target=>target.status==="ready").length,excluded=review.targets.length-eligible;
  const count=document.createElement("p");count.textContent=`${eligible} eligible to update · ${excluded} excluded for revalidation`;
  const tableWrap=document.createElement("div");tableWrap.className="camera-bulk-review-table-wrap";
  const table=document.createElement("table");table.setAttribute("aria-label","Reviewed bulk camera targets");
  const head=document.createElement("thead"),headRow=document.createElement("tr");
  for(const label of ["Camera","Stable ID","Current policy and recording","Planned policy and recording","Review status"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;headRow.append(cell);}head.append(headRow);
  const body=document.createElement("tbody");
  for(const target of review.targets){
    const row=document.createElement("tr");
    for(const value of [target.name,target.id,bulkTargetStateText(target,review.changes),bulkTargetStateText(target,review.changes,true),target.status==="ready"?"Ready":target.status==="archived"?"Archived · excluded":"Missing · excluded"]){const cell=document.createElement("td");cell.textContent=value;row.append(cell);}
    body.append(row);
  }
  table.append(head,body);tableWrap.append(table);
  const actions=document.createElement("div");actions.className="toolbar-row";
  const cancel=document.createElement("button");cancel.type="button";cancel.textContent="Cancel";cancel.addEventListener("click",()=>dialog.close());
  const apply=document.createElement("button");apply.type="button";apply.className="primary";apply.textContent=`Apply to ${eligible} eligible camera(s)`;apply.disabled=!eligible;
  apply.addEventListener("click",()=>{dialog.close();dialog.remove();void executeBulkReview(review);});
  actions.append(cancel,apply);dialog.append(title,intro,changeHeading,changesList);if(policyNote)dialog.append(policyNote);dialog.append(count,tableWrap,actions);document.body.append(dialog);
  dialog.addEventListener("close",()=>dialog.remove());dialog.showModal();title.focus();
}
function ensureBulkResultsDialog(){
  if(cameraBulkStatusDialog)return cameraBulkStatusDialog;
  const dialog=document.createElement("dialog");dialog.id="camera-bulk-results-dialog";dialog.className="camera-bulk-dialog";dialog.setAttribute("aria-labelledby","camera-bulk-results-title");
  const title=document.createElement("h2");title.id="camera-bulk-results-title";title.textContent="Bulk camera results";title.tabIndex=-1;
  const summary=document.createElement("p");summary.id="camera-bulk-results-summary";summary.setAttribute("role","status");summary.setAttribute("aria-live","polite");
  const tableWrap=document.createElement("div");tableWrap.className="camera-bulk-results-table-wrap";
  const table=document.createElement("table");table.setAttribute("aria-label","Per-camera bulk results");
  const head=document.createElement("thead"),headRow=document.createElement("tr");
  for(const label of ["Camera","Stable ID","Outcome","Details"]){const cell=document.createElement("th");cell.scope="col";cell.textContent=label;headRow.append(cell);}head.append(headRow);
  const body=document.createElement("tbody");body.id="camera-bulk-results-body";table.append(head,body);tableWrap.append(table);
  const actions=document.createElement("div");actions.className="toolbar-row";
  const reconcile=document.createElement("button");reconcile.id="camera-bulk-reconcile";reconcile.type="button";reconcile.textContent="Check current inventory";reconcile.hidden=true;reconcile.addEventListener("click",()=>void reconcileBulkOutcomes());
  const retry=document.createElement("button");retry.id="camera-bulk-retry-failed";retry.type="button";retry.textContent="Retry failed only";retry.hidden=true;retry.addEventListener("click",()=>void retryFailedBulkTargets());
  const dismiss=document.createElement("button");dismiss.id="camera-bulk-dismiss-results";dismiss.type="button";dismiss.textContent="Dismiss results";dismiss.hidden=true;dismiss.addEventListener("click",dismissBulkResults);
  const close=document.createElement("button");close.id="camera-bulk-hide-results";close.type="button";close.textContent="Close panel";close.addEventListener("click",()=>dialog.close());
  actions.append(reconcile,retry,dismiss,close);dialog.append(title,summary,tableWrap,actions);document.body.append(dialog);
  dialog.addEventListener("cancel",event=>{event.preventDefault();dialog.close();});
  cameraBulkStatusDialog=dialog;return dialog;
}
function refreshBulkResultsDialog(){
  if(!cameraBulkReview)return;
  const dialog=ensureBulkResultsDialog(),summary=dialog.querySelector("#camera-bulk-results-summary"),body=dialog.querySelector("#camera-bulk-results-body");
  const targetById=new Map(cameraBulkReview.targets.map(target=>[target.id,target]));
  const results=window.NvrCameraBulkActions.orderedResults(cameraBulkReview.cameraIds,cameraBulkResults);
  const totals=results.reduce((counts,result)=>{counts[result.status]=(counts[result.status]||0)+1;return counts;},{});
  const success=totals.success||0,failed=totals.failed||0,unknown=totals.unknown||0,pending=totals.pending||0;
  summary.textContent=cameraBulkOperationInProgress?`Applying reviewed changes; ${pending} result(s) are still pending. This request continues if you close this panel.`:`${success} succeeded · ${failed} failed · ${unknown} unknown and not safe to retry yet.`;
  body.replaceChildren(...results.map(result=>{
    const target=targetById.get(String(result.camera_id)),camera=getCameras().find(item=>item.id===result.camera_id),row=document.createElement("tr");
    const values=[target?.name||camera?.name||"Camera no longer available",result.camera_id,result.status==="unknown"?"Outcome unknown":result.status==="pending"?"Pending":result.status[0].toLocaleUpperCase()+result.status.slice(1),[result.detail,result.warning].filter(Boolean).join(" ")|| (result.status==="pending"?"Waiting for the server to confirm this camera.":"")];
    values.forEach((value,index)=>{const cell=document.createElement("td");cell.textContent=value;cell.className=index===2?"camera-bulk-outcome":"";if(index===2)cell.dataset.status=result.status;row.append(cell);});return row;
  }));
  const retryIds=window.NvrCameraBulkActions.retryableIds(results,getCameras());
  dialog.querySelector("#camera-bulk-retry-failed").hidden=cameraBulkOperationInProgress||retryIds.length===0;
  dialog.querySelector("#camera-bulk-retry-failed").textContent=`Retry ${retryIds.length} failed camera(s) only`;
  dialog.querySelector("#camera-bulk-reconcile").hidden=cameraBulkOperationInProgress||unknown===0;
  dialog.querySelector("#camera-bulk-dismiss-results").hidden=cameraBulkOperationInProgress||pending>0;
  dialog.querySelector("#camera-bulk-hide-results").textContent=cameraBulkOperationInProgress?"Close panel · request continues":"Close panel";
  refreshCameraBulkToolbar();
}
function openBulkResultsDialog(){
  if(!cameraBulkReview)return;
  const dialog=ensureBulkResultsDialog();refreshBulkResultsDialog();
  if(!dialog.open){dialog.showModal();dialog.querySelector("#camera-bulk-results-title").focus();}
}
function dismissBulkResults(){
  if(cameraBulkOperationInProgress)return;
  if(cameraBulkResults.some(result=>result.status!=="success")&&!window.confirm("Dismiss the saved per-camera outcomes? Any failed or unknown target will no longer be available for retry in this session."))return;
  cameraBulkReview=null;cameraBulkResults=[];cameraBulkStatusDialog?.close();refreshCameraBulkToolbar();document.querySelector("#camera-inventory-heading")?.focus();
}
function mergeBulkResults(review,baseResults,updates){
  const byId=new Map((baseResults||[]).map(result=>[String(result.camera_id),result]));
  for(const result of updates||[])byId.set(String(result.camera_id),result);
  return window.NvrCameraBulkActions.orderedResults(review.cameraIds,[...byId.values()]);
}
async function refreshBulkInventory(session=cameraBulkSession){
  const result=await api("/api/cameras");if(session!==cameraBulkSession)return null;
  const cameras=result.cameras||[];setCameras(cameras);renderCameras(cameras);renderSidebar();return cameras;
}
async function reconcileBulkOutcomes(){
  if(!cameraBulkReview||cameraBulkOperationInProgress)return;
  const session=cameraBulkSession;
  const summary=ensureBulkResultsDialog().querySelector("#camera-bulk-results-summary");summary.textContent="Checking current camera settings before allowing any retry…";
  try{
    const cameras=await refreshBulkInventory(session);if(session!==cameraBulkSession||!cameras)return;
    cameraBulkResults=window.NvrCameraBulkActions.reconcileResults(cameraBulkReview.cameraIds,cameraBulkResults,cameras,cameraBulkReview.changes);
    refreshBulkResultsDialog();
  }catch(error){
    if(session!==cameraBulkSession)return;
    summary.textContent=`Current outcomes remain unknown. Inventory could not be checked: ${error.message}. No retry was sent.`;
  }
}
async function executeBulkReview(review,requestedIds=null,priorResults=null){
  if(cameraBulkOperationInProgress)return;
  const session=cameraBulkSession;
  const ids=requestedIds||review.targets.filter(target=>target.status==="ready").map(target=>target.id);
  if(!ids.length)return;
  const base=priorResults||review.targets.map(target=>target.status==="ready"
    ?{camera_id:target.id,status:"pending",detail:"Waiting for the server to confirm this camera."}
    :{camera_id:target.id,status:"failed",code:target.status,detail:target.status==="archived"?"Archived camera excluded during review.":"Camera no longer available during review."});
  const pending=ids.map(camera_id=>({camera_id,status:"pending",detail:"Waiting for the server to confirm this camera."}));
  cameraBulkReview=review;cameraBulkResults=mergeBulkResults(review,base,pending);cameraBulkOperationInProgress=true;
  if(!priorResults)cameraBulkSelection.clear();
  refreshCameraBulkToolbar();renderCameras(getCameras());refreshBulkResultsDialog();openBulkResultsDialog();
  try{
    const response=await api("/api/cameras/bulk-update",{method:"POST",body:JSON.stringify({camera_ids:ids,...review.changes})});
    if(session!==cameraBulkSession)return;
    cameraBulkResults=mergeBulkResults(review,cameraBulkResults,window.NvrCameraBulkActions.orderedResults(ids,response.results));
    try{const cameras=await refreshBulkInventory(session);if(session!==cameraBulkSession||!cameras)return;cameraBulkResults=window.NvrCameraBulkActions.reconcileResults(review.cameraIds,cameraBulkResults,cameras,review.changes);}
    catch{ /* The API's per-camera result remains visible if refreshing the inventory fails. */ }
  }catch(error){
    if(session!==cameraBulkSession)return;
    if(error.status===401||error.status===403){
      const denied=ids.map(camera_id=>({camera_id,status:"failed",code:"unauthorized",detail:"This session is no longer authorized to change cameras. No retry was sent; sign in with administrator access before starting a new review."}));
      cameraBulkResults=mergeBulkResults(review,cameraBulkResults,denied);
    }else{
      const uncertain=ids.map(camera_id=>({camera_id,status:"unknown",code:"outcome_unknown",detail:"The request did not return a confirmed result. Checking current camera settings before allowing retry."}));
      cameraBulkResults=mergeBulkResults(review,cameraBulkResults,uncertain);
      try{const cameras=await refreshBulkInventory(session);if(session!==cameraBulkSession||!cameras)return;cameraBulkResults=window.NvrCameraBulkActions.reconcileResults(review.cameraIds,cameraBulkResults,cameras,review.changes);}
      catch{
        cameraBulkResults=cameraBulkResults.map(result=>result.status==="unknown"?{...result,detail:"Unable to verify this outcome. No automatic retry was sent."}:result);
      }
    }
    const message=document.querySelector("#bulk-message");if(message)message.textContent=`Bulk request response was incomplete: ${error.message}. Check the per-camera outcomes before retrying.`;
  }finally{
    if(session!==cameraBulkSession)return;
    cameraBulkOperationInProgress=false;
    clearConfigDraft(document.querySelector('[data-config-section="bulk-update"]'));
    refreshBulkResultsDialog();
    if(cameraBulkStatusDialog?.open)cameraBulkStatusDialog.querySelector("#camera-bulk-hide-results")?.focus();
  }
}
async function retryFailedBulkTargets(){
  if(!cameraBulkReview||cameraBulkOperationInProgress)return;
  const session=cameraBulkSession;
  try{
    const cameras=await refreshBulkInventory(session);if(session!==cameraBulkSession||!cameras)return;
    cameraBulkResults=window.NvrCameraBulkActions.reconcileResults(cameraBulkReview.cameraIds,cameraBulkResults,cameras,cameraBulkReview.changes);
    const ids=window.NvrCameraBulkActions.retryableIds(cameraBulkResults,cameras);
    if(!ids.length){refreshBulkResultsDialog();return;}
    await executeBulkReview(cameraBulkReview,ids,cameraBulkResults);
  }catch(error){
    if(session!==cameraBulkSession)return;
    const summary=ensureBulkResultsDialog().querySelector("#camera-bulk-results-summary");summary.textContent=`Current camera status could not be verified: ${error.message}. No retry was sent.`;
  }
}


    root.querySelector("#camera-bulk-select-matching")?.addEventListener("click",()=>{cameraBulkVisibleIds.forEach(id=>cameraBulkSelection.add(id));renderCameras(getCameras());});
    root.querySelector("#camera-bulk-clear-selection")?.addEventListener("click",()=>{cameraBulkSelection.clear();renderCameras(getCameras());});
    root.querySelector("#camera-bulk-view-results")?.addEventListener("click",openBulkResultsDialog);
    root.querySelector("#apply-bulk")?.addEventListener("click",openBulkReviewDialog);
    refreshCameraBulkToolbar();
    return {
      setVisibleIds(ids){cameraBulkVisibleIds=[...new Set((ids||[]).map(String))];refreshCameraBulkToolbar();},
      isSelected(id){return cameraBulkSelection.has(String(id));},
      toggleSelection(id,selected){if(selected)cameraBulkSelection.add(String(id));else cameraBulkSelection.delete(String(id));refreshCameraBulkToolbar();},
      isBusy(){return cameraBulkOperationInProgress;},
      refreshToolbar:refreshCameraBulkToolbar,
      resetSession:resetCameraBulkSession,
      openResults:openBulkResultsDialog,
      selectVisible(){cameraBulkVisibleIds.forEach(id=>cameraBulkSelection.add(id));renderCameras(getCameras());},
      clearSelection(){cameraBulkSelection.clear();renderCameras(getCameras());},
    };
  }

  return { selectionCounts, createReview, orderedResults, reconcileResults, retryableIds, plannedState, mountWorkspace };
});
