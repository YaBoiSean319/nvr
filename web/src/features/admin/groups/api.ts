/** Policy group requests. */
import { api } from "@/api/client";
import type { CameraGroup } from "@/api/types";
import type { ScheduleInterval, Zone } from "@/features/admin/cameras/api";

const at = (id: string) => `/api/camera-groups/${encodeURIComponent(id)}`;

export interface GroupSchedule { group_id: string; configured: boolean; timezone: string; intervals: ScheduleInterval[] }
export interface GroupRetention { group_id: string; configured: boolean; retention_days: number; policy_source: string }
export interface GroupStorage { group_id: string; volume_id: string; configured_volumes: string[] }
export interface GroupAi {
  group_id: string; configured: boolean; enabled: boolean; analysis_fps: number; confidence_threshold: number; use_substream: boolean; zones: Zone[];
  camera_count: number; inherited_camera_count: number;
}

export const createGroup = (name: string) => api<{ id: string; name: string }>("/api/camera-groups", { method: "POST", body: { name } });
export const renameGroup = (id: string, name: string) => api<CameraGroup>(at(id), { method: "PATCH", body: { name } });
export const deleteGroup = (id: string) => api<void>(at(id), { method: "DELETE" });
export const setMembers = (id: string, camera_ids: string[]) => api<CameraGroup>(`${at(id)}/members`, { method: "PUT", body: { camera_ids } });
export const getGroupSchedule = (id: string, signal?: AbortSignal) => api<GroupSchedule>(`${at(id)}/schedule`, { signal });
export const putGroupSchedule = (id: string, body: { timezone: string; intervals: ScheduleInterval[] }) => api<GroupSchedule>(`${at(id)}/schedule`, { method: "PUT", body });
export const deleteGroupSchedule = (id: string) => api<void>(`${at(id)}/schedule`, { method: "DELETE" });
export const getGroupRetention = (id: string, signal?: AbortSignal) => api<GroupRetention>(`${at(id)}/retention`, { signal });
export const putGroupRetention = (id: string, retention_days: number) => api<GroupRetention>(`${at(id)}/retention`, { method: "PUT", body: { retention_days } });
export const deleteGroupRetention = (id: string) => api<void>(`${at(id)}/retention`, { method: "DELETE" });
export const getGroupStorage = (id: string, signal?: AbortSignal) => api<GroupStorage>(`${at(id)}/storage-volume`, { signal });
export const putGroupStorage = (id: string, volume_id: string | null) => api<GroupStorage>(`${at(id)}/storage-volume`, { method: "PUT", body: { volume_id } });
export const getGroupAi = (id: string, signal?: AbortSignal) => api<GroupAi>(`${at(id)}/ai`, { signal });
export const putGroupAi = (id: string, body: Pick<GroupAi, "enabled" | "analysis_fps" | "confidence_threshold" | "use_substream" | "zones">) =>
  api<GroupAi>(`${at(id)}/ai`, { method: "PUT", body });
