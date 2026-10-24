import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { keys, useCameraGroups } from "@/api/queries";
import type { Camera } from "@/api/types";
import { Modal } from "@/components/Modal";
import type { BulkResult, ScheduleMode } from "./api";
import { applied, consequences, describeAction, review, type Batch, type BulkAction, type CameraOutcome } from "./bulk";
import { useCamerasStore } from "./store";

const OUTCOME_LABEL: Record<CameraOutcome["outcome"], string> = {
  pending: "Waiting", success: "Changed", failed: "Not changed", unknown: "Checking…", excluded: "Excluded",
};

type Choice = { group: "keep" | "none" | "existing" | "new"; groupName: string; newGroup: string; mode: "keep" | ScheduleMode; enabled: "keep" | "on" | "off" };

/** Choose a change, review the exact cameras, apply, then see and retry per-camera results. */
export function BulkDialog({ cameras }: { cameras: Camera[] }) {
  const client = useQueryClient();
  const groups = useCameraGroups();
  const open = useCamerasStore(state => state.bulkOpen);
  const batch = useCamerasStore(state => state.batch);
  const checked = useCamerasStore(state => state.checked);
  const { setBulkOpen, setBatch } = useCamerasStore.getState();
  const [choice, setChoice] = useState<Choice>({ group: "keep", groupName: "", newGroup: "", mode: "keep", enabled: "keep" });

  const action: BulkAction = {
    ...(choice.group === "none" ? { group: null } : choice.group === "existing" && choice.groupName ? { group: choice.groupName } : choice.group === "new" && choice.newGroup.trim() ? { group: choice.newGroup.trim() } : {}),
    ...(choice.mode !== "keep" ? { recording_mode: choice.mode } : {}),
    ...(choice.enabled !== "keep" ? { recording_enabled: choice.enabled === "on" } : {}),
  };
  const changes = describeAction(action);

  async function run(target: Batch, ids: string[]) {
    const update = (outcomes: CameraOutcome[]) => {
      const latest = { ...target, outcomes, phase: outcomes.some(item => item.outcome === "pending" || item.outcome === "unknown") ? "running" as const : "done" as const };
      setBatch(latest);
      return latest;
    };
    let outcomes = target.outcomes.map(item => (ids.includes(item.id) ? { ...item, outcome: "pending" as const, detail: null } : item));
    update(outcomes);
    const body = {
      camera_ids: ids,
      ...(target.action.group !== undefined ? { group_name: target.action.group } : {}),
      ...(target.action.recording_mode ? { recording_mode: target.action.recording_mode } : {}),
      ...(target.action.recording_enabled !== undefined ? { recording_enabled: target.action.recording_enabled } : {}),
    };
    try {
      const response = await api<BulkResult>("/api/cameras/bulk-update", { method: "POST", body });
      const byId = new Map(response.results.map(result => [result.camera_id, result]));
      outcomes = outcomes.map(item => {
        const result = byId.get(item.id);
        if (!ids.includes(item.id) || !result) return item;
        if (result.status === "success") return { ...item, outcome: "success", detail: result.warning ?? null };
        return { ...item, outcome: result.status === "unknown" ? "unknown" : "failed", detail: result.detail ?? null };
      });
    } catch {
      // The request may or may not have reached the NVR: every camera's outcome is unknown until checked.
      outcomes = outcomes.map(item => (ids.includes(item.id) ? { ...item, outcome: "unknown", detail: "The NVR's answer was lost." } : item));
    }
    update(outcomes);
    if (outcomes.some(item => item.outcome === "unknown")) {
      // Reconcile against what the NVR now reports before anything can be retried.
      try {
        const fresh = await api<{ cameras: Camera[] }>("/api/cameras");
        const now = new Map(fresh.cameras.map(camera => [camera.id, camera]));
        outcomes = outcomes.map(item => item.outcome !== "unknown" ? item
          : applied(now.get(item.id), target.action)
            ? { ...item, outcome: "success", detail: "Confirmed after an unclear response." }
            : { ...item, outcome: "failed", detail: "Confirmed not changed after an unclear response." });
      } catch {
        outcomes = outcomes.map(item => (item.outcome === "unknown" ? { ...item, detail: "Could not confirm; refresh before retrying." } : item));
      }
      update(outcomes);
    }
    void client.invalidateQueries({ queryKey: keys.cameras });
    void client.invalidateQueries({ queryKey: ["camera-groups"] });
  }

  function startReview() {
    setBatch(review(checked, cameras, action));
  }

  const counts = batch ? {
    success: batch.outcomes.filter(item => item.outcome === "success").length,
    failed: batch.outcomes.filter(item => item.outcome === "failed").length,
    excluded: batch.outcomes.filter(item => item.outcome === "excluded").length,
    eligible: batch.outcomes.filter(item => item.outcome === "pending").length,
  } : null;
  const finished = batch?.phase === "done";
  const running = batch?.phase === "running";

  function close() {
    setBulkOpen(false);
    if (finished || batch?.phase === "review") setBatch(finished ? batch : null);
  }

  const footer = !batch ? (
    <>
      <button type="button" className="button" onClick={close}>Cancel</button>
      <button type="button" className="button" data-variant="primary" disabled={!changes.length || !checked.length} onClick={startReview}>Review {checked.length} camera{checked.length === 1 ? "" : "s"}</button>
    </>
  ) : batch.phase === "review" ? (
    <>
      <button type="button" className="button" onClick={() => setBatch(null)}>Back</button>
      <button type="button" className="button" data-variant="primary" disabled={!counts!.eligible}
              onClick={() => void run(batch, batch.outcomes.filter(item => item.outcome === "pending").map(item => item.id))}>
        Apply to {counts!.eligible} camera{counts!.eligible === 1 ? "" : "s"}
      </button>
    </>
  ) : (
    <>
      {finished && counts!.failed ? (
        <button type="button" className="button" onClick={() => void run(batch, batch.outcomes.filter(item => item.outcome === "failed").map(item => item.id))}>
          Retry {counts!.failed} failed
        </button>
      ) : null}
      {finished ? <button type="button" className="button" onClick={() => { setBatch(null); close(); }}>Done</button> : null}
      <button type="button" className="button" onClick={close}>{running ? "Hide (keeps running)" : "Close"}</button>
    </>
  );

  return (
    <Modal open={open} onOpenChange={value => !value && close()} wide title={!batch ? "Bulk change" : batch.phase === "review" ? "Review bulk change" : "Bulk change results"}
           description={!batch ? `Change ${checked.length} selected camera${checked.length === 1 ? "" : "s"}. Nothing changes until you review and apply.` : describeAction(batch.action).join(" · ")}
           footer={footer}>
      {!batch ? (
        <div className="tab-form">
          <fieldset className="field-group">
            <legend>Policy group</legend>
            <div className="segmented" role="radiogroup" aria-label="Policy group change">
              <label><input type="radio" checked={choice.group === "keep"} onChange={() => setChoice({ ...choice, group: "keep" })} /> Keep</label>
              <label><input type="radio" checked={choice.group === "existing"} onChange={() => setChoice({ ...choice, group: "existing" })} /> Move to group</label>
              <label><input type="radio" checked={choice.group === "new"} onChange={() => setChoice({ ...choice, group: "new" })} /> New group</label>
              <label><input type="radio" checked={choice.group === "none"} onChange={() => setChoice({ ...choice, group: "none" })} /> No group</label>
            </div>
            {choice.group === "existing" ? (
              <select aria-label="Target group" value={choice.groupName} onChange={event => setChoice({ ...choice, groupName: event.target.value })}>
                <option value="">Choose a group…</option>
                {(groups.data ?? []).map(group => <option key={group.id} value={group.name}>{group.name}</option>)}
              </select>
            ) : null}
            {choice.group === "new" ? <input aria-label="New group name" value={choice.newGroup} maxLength={80} onChange={event => setChoice({ ...choice, newGroup: event.target.value })} /> : null}
          </fieldset>
          <div className="field-row">
            <label className="field"><span>Recording mode</span>
              <select value={choice.mode} onChange={event => setChoice({ ...choice, mode: event.target.value as Choice["mode"] })}>
                <option value="keep">Keep</option><option value="motion">Motion</option><option value="continuous">Continuous</option><option value="disabled">Disabled</option>
              </select>
            </label>
            <label className="field"><span>Recording</span>
              <select value={choice.enabled} onChange={event => setChoice({ ...choice, enabled: event.target.value as Choice["enabled"] })}>
                <option value="keep">Keep</option><option value="on">Turn on</option><option value="off">Turn off</option>
              </select>
            </label>
          </div>
        </div>
      ) : (
        <>
          {batch.phase === "review" ? consequences(batch.action).map(note => <p key={note} className="notice">{note}</p>) : null}
          {running ? <p className="notice" role="status">Applying… Closing this dialog does not stop the change.</p> : null}
          {finished ? (
            <p className="notice" data-tone={counts!.failed ? "warn" : "good"} role="status">
              {counts!.success} changed, {counts!.failed} not changed{counts!.excluded ? `, ${counts!.excluded} excluded` : ""}.
            </p>
          ) : null}
          <table className="inventory-table bulk-table" aria-label="Cameras in this change">
            <thead><tr><th scope="col">Camera</th><th scope="col">Current group</th><th scope="col">Outcome</th></tr></thead>
            <tbody>
              {batch.outcomes.map(item => {
                const camera = cameras.find(entry => entry.id === item.id);
                return (
                  <tr key={item.id} data-outcome={item.outcome}>
                    <td>{item.name}</td>
                    <td>{camera?.group_name ?? "None"}</td>
                    <td>{batch.phase === "review" && item.outcome === "pending" ? "Will change" : OUTCOME_LABEL[item.outcome]}{item.detail ? <span className="muted"> · {item.detail}</span> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </Modal>
  );
}
