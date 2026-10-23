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

export interface SavedView {
  name: string;
  scope: "personal" | "shared";
  camera_ids: string[];
  grid_size: number;
  tile_camera_ids: (string | null)[];
  layout_mode: "auto" | "manual";
}

export interface CameraGroup {
  id: string;
  name: string;
  camera_ids: string[];
  camera_count: number;
}

export interface LiveWorkspaceState {
  camera_ids: string[] | null;
  tile_camera_ids: (string | null)[];
  grid_size: number;
  layout_mode: "auto" | "manual";
  sidebar_collapsed: boolean;
}

export interface DisplaySettings {
  time_zone: string;
  date_format: "friendly" | "long" | "iso" | "day-month" | "month-day";
  clock_format: "12" | "24";
}

export type GapState = "failure" | "uncertain" | "expected_idle" | "missing";

export interface TimelineCamera {
  camera_id: string;
  name: string;
  recording_status: string;
  segments: { id: number; starts_at: string; ends_at: string; size_bytes: number; media_url: string }[];
  gaps: { starts_at: string; ends_at: string; state: GapState; reason: string }[];
  motion_events: { id: string; source_event_at: string; status: string }[];
  ai_detections: { id: string; label: "person" | "vehicle"; confidence: number; first_seen_at: string; last_seen_at: string }[];
  ai_unanalyzed: { state: string; starts_at: string; ends_at: string | null; detail: string }[];
}

export interface PlaybackTimeline {
  start: string;
  end: string;
  cameras: TimelineCamera[];
}
