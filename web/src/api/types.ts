export type Role = "administrator" | "operator";
export type ThemePreference = "system" | "light" | "dark";

export interface Me {
  username: string;
  role: Role;
  /** The administrator requires a second factor and this account has none yet: only enrolment is open. */
  mfa_enrolment_required?: boolean;
}

/** The password was right but the account has a second factor to answer before it gets a session. */
export interface MfaChallenge {
  mfa_required: true;
  challenge: string;
  methods: ("passkey" | "totp" | "recovery")[];
}

export interface SecuritySummary {
  local: boolean;
  policy: "off" | "administrators" | "all";
  required: boolean;
  totp: boolean;
  passkeys: { id: number; name: string; created_at: string; last_used_at: string | null }[];
  recovery_codes_remaining: number;
  passkey_sign_in: boolean;
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
  camera_brand: CameraBrand;
  device_service_url: string | null;
  onvif_profile_token: string | null;
  live_substream_profile_token: string | null;
  /** Masked: credentials are never returned. */
  stream_url: string;
  video: { codec: string | null; width: number | null; height: number | null };
  audio_supported: boolean | null;
  last_probe_at: string | null;
  last_error: string | null;
  last_footage_write_at: string | null;
  effective_recording_mode?: "motion" | "continuous" | "disabled";
  recording_policy_source?: string;
  schedule_timezone?: string;
  retention_days?: number;
  retention_policy_source?: string;
  storage_volume_id: string | null;
  recording_fault_since: string | null;
  motion_pre_roll_seconds: number;
  motion_post_roll_seconds: number;
  motion_fallback_enabled: boolean;
  motion_zones: { x: number; y: number; width: number; height: number }[];
  motion_sensitivity: number;
  motion_source_configured: boolean;
  motion_source_status: string;
  server_motion_status: string;
  audio_status: "enabled" | "available_disabled" | "unsupported" | "unknown";
}

export type CameraBrand = "auto" | "acti" | "axis" | "bosch" | "dahua" | "hanwha" | "hikvision" | "reolink" | "samsung" | "uniview" | "other";

export interface OnvifProfile {
  token: string;
  name: string;
  encoding?: string | null;
  width?: number | null;
  height?: number | null;
}

/** One step of a camera inspection, as the NVR reports it. */
export interface InspectStep { id: string; label: string; state: "pending" | "ok" | "failed" | "skipped"; detail: string }
export interface InspectCapability { state: "ok" | "available" | "unsupported" | "failed" | "unknown"; message: string }
export interface InspectProfile { token: string; name: string; encoding: string; width: number; height: number; fps: number; has_audio: boolean }

/** What `POST /api/cameras/inspect` says about a camera. It never holds a login or an address with one. */
export interface InspectReport {
  ok: boolean;
  device: { manufacturer?: string; model?: string; firmware?: string };
  brand: CameraBrand;
  device_service_url: string | null;
  profiles: InspectProfile[];
  selection: { record_token: string; live_token: string | null; reason: string; warnings: string[]; live_verified?: boolean | null } | null;
  steps: InspectStep[];
  capabilities: Record<string, InspectCapability>;
  diagnostics: Record<string, string | number | null>[];
}

export interface StreamProbe {
  reachable: boolean;
  reason?: string;
  video?: { codec: string | null; width: number | null; height: number | null };
  audio?: unknown;
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
  completed_at: string | null;
  start: string;
  end: string;
  camera_ids: string[];
  output_modes: ("individual" | "grid")[];
  grid_resolution: "1080p" | "4k";
  include_audio: boolean;
  expires_at: string | null;
  download_url: string | null;
  cancel_requested: boolean;
  retry_of: string | null;
  redaction_review_id: string | null;
}

export interface RedactionTrack {
  id: string;
  camera_id: string;
  kind: "face" | "plate";
  start: number;
  end: number;
  confidence: number;
  blur: boolean;
  box: { x: number; y: number; width: number; height: number };
}

export interface RedactionKeyframe { t: number; x: number; y: number; width: number; height: number }

export interface RedactionRegion {
  id: string;
  camera_id: string;
  start: number;
  end: number;
  follow_track_id: string | null;
  scale: number;
  keyframes: RedactionKeyframe[];
}

export interface RedactionReview {
  id: string;
  state: "queued" | "analysing" | "ready" | "confirmed" | "used" | "failed";
  progress: number;
  error: string | null;
  camera_ids: string[];
  start: string;
  end: string;
  kinds: ("faces" | "plates")[];
  durations: Record<string, number>;
  tracks: RedactionTrack[];
  manual: RedactionRegion[];
  confirmed_at: string | null;
  edited_at: string | null;
}

export interface RedactionEdit {
  keep?: string[];
  manual?: Omit<RedactionRegion, "id">[];
}

export interface ExportRequest {
  camera_ids: string[];
  start: string;
  end: string;
  output_modes: ("individual" | "grid")[];
  tile_camera_ids: (string | null)[];
  grid_resolution: "1080p" | "4k";
  include_audio: boolean;
  audio_camera_id: string | null;
  redaction_review_id?: string | null;
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

export type GapReason = "camera_offline" | "stream_error" | "disk_unavailable" | "outside_schedule" | "recording_disabled" | "worker_restart" | "no_motion" | "unknown";

export interface TimelineCamera {
  camera_id: string;
  name: string;
  recording_status: string;
  segments: { id: number; starts_at: string; ends_at: string; size_bytes: number; media_url: string }[];
  gaps: { starts_at: string; ends_at: string; state: GapState; reason: string; reason_code: GapReason }[];
  motion_events: { id: string; source_event_at: string; status: string }[];
  ai_detections: { id: string; label: "person" | "vehicle"; confidence: number; first_seen_at: string; last_seen_at: string }[];
  ai_unanalyzed: { state: string; starts_at: string; ends_at: string | null; detail: string }[];
}

export interface PlaybackTimeline {
  start: string;
  end: string;
  cameras: TimelineCamera[];
}
