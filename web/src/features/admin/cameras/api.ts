/** Camera administration requests. Credentials travel only in request bodies, never in URLs. */
import { api } from "@/api/client";
import type { BackfillStatus, Camera, CameraBrand, OnvifProfile, StreamProbe } from "@/api/types";

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

export interface Zone { x: number; y: number; width: number; height: number }
export type ScheduleMode = "motion" | "continuous" | "disabled";
export interface ScheduleInterval { days: number[]; start: string; end: string; mode: ScheduleMode }

export interface CameraSchedule {
  camera_id: string;
  inherits_group: boolean;
  source: "camera" | "group";
  configured: boolean;
  timezone: string;
  intervals: ScheduleInterval[];
  effective_recording_mode: ScheduleMode;
}

export interface CameraRetention {
  camera_id: string;
  inherits_group: boolean;
  retention_days: number;
  policy_source: string;
  oldest_footage_at?: string | null;
  newest_footage_at?: string | null;
  total_bytes?: number;
  [key: string]: unknown;
}

export interface CameraAi {
  enabled: boolean;
  analysis_fps: number;
  confidence_threshold: number;
  use_substream: boolean;
  zones: Zone[];
  config_source: string;
  config_override: boolean;
  status: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  model: { model_id: string; model_version: string; runtime: string; available: boolean; reason: string | null };
  coverage_note: string;
}

export interface AiCoverage {
  status: string;
  last_error: string | null;
  intervals: { state: string; starts_at: string; ends_at: string | null; detail: string }[];
  coverage_note: string;
}

export interface MotionSettings {
  service_url: string | null;
  username: string | null;
  password: string | null;
  fallback_enabled: boolean;
  sensitivity: number;
  zones: Zone[];
}

export interface BulkResult {
  cameras: Camera[];
  results: { camera_id: string; status: "success" | "failed" | "unknown"; code: string | null; detail?: string; warning?: string }[];
}

export const getSchedule = (id: string, signal?: AbortSignal) => api<CameraSchedule>(`${at(id)}/schedule`, { signal });
export const putSchedule = (id: string, body: { timezone: string; intervals: ScheduleInterval[]; inherit_group: boolean }) =>
  api<CameraSchedule>(`${at(id)}/schedule`, { method: "PUT", body });
export const getRetention = (id: string, signal?: AbortSignal) => api<CameraRetention>(`${at(id)}/retention`, { signal });
export const putRetention = (id: string, retention_days: number) => api<CameraRetention>(`${at(id)}/retention`, { method: "PUT", body: { retention_days } });
export const deleteRetention = (id: string) => api<void>(`${at(id)}/retention`, { method: "DELETE" });
export const getAi = (id: string, signal?: AbortSignal) => api<CameraAi>(`${at(id)}/ai`, { signal });
export const putAi = (id: string, body: Pick<CameraAi, "enabled" | "analysis_fps" | "confidence_threshold" | "use_substream" | "zones"> & { inherit_group: boolean }) =>
  api<CameraAi>(`${at(id)}/ai`, { method: "PUT", body });
export const getAiCoverage = (id: string, signal?: AbortSignal) => api<AiCoverage>(`${at(id)}/ai/coverage`, { signal });
export const updateRecording = (id: string, body: { recording_enabled?: boolean; recording_mode?: ScheduleMode; motion_pre_roll_seconds?: number; motion_post_roll_seconds?: number; audio_enabled?: boolean }) =>
  api<Camera>(at(id), { method: "PATCH", body });
export const probeAudio = (id: string) => api<{ audio_supported: boolean; audio_enabled: boolean; audio_status: string; detail: string }>(`${at(id)}/audio/probe`, { method: "POST" });
export const configureMotion = (id: string, body: Partial<Omit<MotionSettings, "username" | "password">> & { username?: string; password?: string }) =>
  api<Camera>(`${at(id)}/motion-source`, { method: "POST", body });
export const assignGroup = (id: string, group_name: string | null) =>
  api<BulkResult>("/api/cameras/bulk-update", { method: "POST", body: { camera_ids: [id], group_name } });
export const getCoverage = (id: string, signal?: AbortSignal) => api<Record<string, unknown>>(`${at(id)}/coverage`, { signal });
export const getMotionEvents = (id: string, signal?: AbortSignal) =>
  api<{ events: { id: string; source_event_at: string; status: string; failure_reason: string | null }[] }>(`${at(id)}/motion-events`, { signal });
export const latestSegment = (id: string, signal?: AbortSignal) =>
  api<{ segments: { id: number; starts_at: string; ends_at: string }[] }>(`${at(id)}/segments`, { signal }).then(result => result.segments.at(-1) ?? null);

export type TamperState = "off" | "learning" | "ok" | "covered" | "moved" | "defocused";
export interface CameraTamper {
  enabled: boolean; sensitivity: number; sustain_seconds: number; state: TamperState; state_since: string | null;
  learning_progress: number; learning_samples: number; reference_at: string | null; last_sample_at: string | null;
  last_metrics: { layout_match: number; brightness: number; contrast: number; sharpness: number; judged: string | null } | null; sample_seconds: number;
}
export const getTamper = (id: string, signal?: AbortSignal) => api<CameraTamper>(`${at(id)}/tamper`, { signal });
export const putTamper = (id: string, body: Pick<CameraTamper, "enabled" | "sensitivity" | "sustain_seconds">) => api<CameraTamper>(`${at(id)}/tamper`, { method: "PUT", body });
export const relearnTamper = (id: string) => api<CameraTamper>(`${at(id)}/tamper/relearn`, { method: "POST" });

export interface RetentionForecast {
  camera: { id: string; name: string; policy_days: number; forecast_days: number | null; state: "ok" | "below_policy" | "not_forecast"; below_policy: boolean;
            days_until_shortfall: number | null; message: string; suggestions: string[] };
  warn_days: number;
}
export const getRetentionForecast = (id: string, signal?: AbortSignal) => api<RetentionForecast>(`${at(id)}/retention-forecast`, { signal });

export const getBackfill = (id: string, signal?: AbortSignal) => api<BackfillStatus>(`${at(id)}/backfill`, { signal });
export const saveBackfill = (id: string, body: { enabled: boolean; limit_kbps: number | null }) => api<BackfillStatus>(`${at(id)}/backfill`, { method: "PUT", body });
export const checkBackfill = (id: string) => api<BackfillStatus>(`${at(id)}/backfill/check`, { method: "POST" });
