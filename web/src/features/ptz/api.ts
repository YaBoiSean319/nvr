import { api } from "@/api/client";

export interface PtzLease { holder: string; mine: boolean; acquired_at: string; expires_at: string }
export interface PtzPreset { id: string; name: string; token: string }
export interface TourStep { preset_id: string; dwell_seconds: number }
export interface TourSchedule { timezone: string; intervals: { days: number[]; start: string; end: string }[] }
export interface PtzTour { id: string; name: string; enabled: boolean; steps: TourStep[]; schedule: TourSchedule | null; state: "idle" | "running" | "paused"; step: number }
export interface PtzState {
  supported: boolean;
  reason: string | null;
  capabilities: { pan_tilt: boolean; zoom: boolean; centre: boolean; max_presets: number } | null;
  lease: PtzLease | null;
  lease_seconds: number;
  presets: PtzPreset[];
  tours: PtzTour[];
}
export type TourDefinition = Pick<PtzTour, "name" | "enabled" | "steps" | "schedule">;

const at = (cameraId: string, path = "") => `/api/cameras/${encodeURIComponent(cameraId)}/ptz${path}`;

export const ptzKey = (cameraId: string) => ["ptz", cameraId] as const;
export const getPtz = (cameraId: string, signal?: AbortSignal) => api<PtzState>(at(cameraId), { signal });
export const move = (cameraId: string, pan: number, tilt: number, zoom: number) =>
  api<{ lease: PtzLease }>(at(cameraId, "/move"), { method: "POST", body: { pan, tilt, zoom, seconds: 1.5 } });
export const stop = (cameraId: string) => api<{ lease: PtzLease }>(at(cameraId, "/stop"), { method: "POST" });
export const centre = (cameraId: string, x: number, y: number) => api<{ lease: PtzLease }>(at(cameraId, "/centre"), { method: "POST", body: { x, y } });
export const goToPreset = (cameraId: string, presetId: string) => api<{ lease: PtzLease }>(at(cameraId, `/presets/${presetId}/go`), { method: "POST" });
export const takeOver = (cameraId: string) => api<PtzLease>(at(cameraId, "/lease"), { method: "POST", body: { take_over: true } });
export const releaseLease = (cameraId: string) => api<void>(at(cameraId, "/lease"), { method: "DELETE" });
export const createPreset = (cameraId: string, name: string) => api<PtzPreset>(at(cameraId, "/presets"), { method: "POST", body: { name } });
export const renamePreset = (cameraId: string, id: string, name: string) => api<PtzPreset>(at(cameraId, `/presets/${id}`), { method: "PATCH", body: { name } });
export const deletePreset = (cameraId: string, id: string) => api<void>(at(cameraId, `/presets/${id}`), { method: "DELETE" });
export const saveTour = (cameraId: string, tour: TourDefinition, id?: string) =>
  api<PtzTour>(at(cameraId, id ? `/tours/${id}` : "/tours"), { method: id ? "PUT" : "POST", body: tour });
export const deleteTour = (cameraId: string, id: string) => api<void>(at(cameraId, `/tours/${id}`), { method: "DELETE" });
