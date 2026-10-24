/** Reviewed bulk changes: a fixed camera set, per-camera outcomes and honest reconciliation. */
import type { Camera } from "@/api/types";
import type { ScheduleMode } from "./api";

export interface BulkAction {
  /** undefined: unchanged; null: remove from any group; string: that group (created if new). */
  group?: string | null;
  recording_mode?: ScheduleMode;
  recording_enabled?: boolean;
}

export type Outcome = "pending" | "success" | "failed" | "unknown" | "excluded";

export interface CameraOutcome {
  id: string;
  name: string;
  outcome: Outcome;
  detail: string | null;
}

export interface Batch {
  action: BulkAction;
  /** The reviewed set; applying never recomputes it from filters. */
  outcomes: CameraOutcome[];
  phase: "review" | "running" | "done";
}

export function describeAction(action: BulkAction): string[] {
  const changes: string[] = [];
  if (action.group !== undefined) changes.push(action.group ? `Move to policy group ${action.group}` : "Remove from their policy group");
  if (action.recording_mode) changes.push(`Set recording mode to ${action.recording_mode}`);
  if (action.recording_enabled !== undefined) changes.push(action.recording_enabled ? "Turn recording on" : "Turn recording off");
  return changes;
}

export function consequences(action: BulkAction): string[] {
  const notes: string[] = [];
  if (action.group) notes.push(`Cameras without their own overrides will follow ${action.group}'s schedule, retention, storage volume and AI settings.`);
  if (action.group === null) notes.push("Cameras without their own overrides will use the system defaults for schedule, retention, storage and AI.");
  if (action.recording_enabled && action.recording_mode !== "disabled") notes.push("Cameras whose recording mode is disabled stay off.");
  if (action.recording_mode === "disabled") notes.push("Disabled mode also turns recording off.");
  return notes;
}

/** Build the reviewed set: archived or missing cameras are excluded with a reason. */
export function review(ids: string[], cameras: Camera[], action: BulkAction): Batch {
  const byId = new Map(cameras.map(camera => [camera.id, camera]));
  const outcomes = ids.map<CameraOutcome>(id => {
    const camera = byId.get(id);
    if (!camera) return { id, name: "Removed camera", outcome: "excluded", detail: "This camera is no longer available." };
    if (camera.archived_at) return { id, name: camera.name, outcome: "excluded", detail: "Archived cameras are excluded from bulk changes." };
    return { id, name: camera.name, outcome: "pending", detail: null };
  });
  return { action, outcomes, phase: "review" };
}

/** Whether a camera now shows the change, for outcomes the server could not confirm. */
export function applied(camera: Camera | undefined, action: BulkAction): boolean {
  if (!camera) return false;
  if (action.group !== undefined && (camera.group_name ?? null) !== (action.group || null)) return false;
  if (action.recording_mode && camera.recording_mode !== action.recording_mode) return false;
  if (action.recording_enabled !== undefined) {
    const expected = action.recording_enabled && camera.recording_mode !== "disabled";
    if (camera.recording_enabled !== expected) return false;
  }
  return true;
}
