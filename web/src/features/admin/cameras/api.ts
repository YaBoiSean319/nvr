/** Camera administration requests. Credentials travel only in request bodies, never in URLs. */
import { api } from "@/api/client";
import type { Camera, CameraBrand, OnvifProfile, StreamProbe } from "@/api/types";

export interface SourceDraft {
  camera_ip: string | null;
  stream_url: string | null;
  camera_username: string | null;
  camera_password: string | null;
  camera_brand: CameraBrand;
  device_service_url: string | null;
  onvif_profile_token: string | null;
  live_substream_profile_token: string | null;
  clear_credentials: boolean;
}

const at = (id: string) => `/api/cameras/${encodeURIComponent(id)}`;

export const renameCamera = (id: string, name: string, signal?: AbortSignal) =>
  api<Camera>(at(id), { method: "PATCH", body: { name }, signal });
export const updateSource = (id: string, source: SourceDraft, signal?: AbortSignal) =>
  api<Camera>(`${at(id)}/source`, { method: "PUT", body: source, signal });
export const testSource = (id: string, source: SourceDraft, signal?: AbortSignal) =>
  api<StreamProbe>(`${at(id)}/source/test`, { method: "POST", body: source, signal });
export const loadProfiles = (id: string, source: SourceDraft, signal?: AbortSignal) =>
  api<{ device_service_url: string | null; profiles: OnvifProfile[] }>(`${at(id)}/source/profiles`, { method: "POST", body: source, signal });
export const archiveCamera = (id: string) => api<void>(at(id), { method: "DELETE" });
export const restoreCamera = (id: string) => api<Camera>(`${at(id)}/restore`, { method: "POST" });
