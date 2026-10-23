export type Role = "administrator" | "operator";
export type ThemePreference = "system" | "light" | "dark";

export interface Me {
  username: string;
  role: Role;
}

export interface Branding {
  app_name: string;
  primary_color: string;
  accent_color: string;
  assets: Partial<Record<"primary" | "light" | "dark" | "icon", { url: string; media_type: string; sha256: string }>>;
  updated_at: string | null;
}

export type RecordingHealthState =
  | "recording"
  | "expected_idle"
  | "camera_unreachable"
  | "uncertain"
  | "write_stalled"
  | "motion_source_degraded";

export interface Camera {
  id: string;
  name: string;
  group_name: string | null;
  camera_ip: string | null;
  archived_at: string | null;
  enabled: boolean;
  recording_mode: "motion" | "continuous" | "disabled";
  recording_enabled: boolean;
  recording_status: string;
  last_video_at: string | null;
  audio_enabled: boolean;
  live_substream_available: boolean;
  recording_health: { state: RecordingHealthState; detail: string; video_fresh: boolean };
}

export interface Incident {
  id: string;
  camera_id: string | null;
  fault_type: string;
  summary: string;
  state: "active" | "acknowledged" | "recovered";
  fault_active: boolean;
  recurrence_count: number;
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_at: string | null;
  recovered_at: string | null;
}

export interface ExportJob {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  error: string | null;
  created_at: string;
}
