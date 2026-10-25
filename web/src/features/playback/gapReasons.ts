import type { GapReason } from "@/api/types";

/** Why a stretch of the timeline has no footage, as the recorder recorded it. */
export const REASON_LABEL: Record<GapReason, string> = {
  camera_offline: "Camera offline",
  stream_error: "Stream error",
  disk_unavailable: "Disk full or unavailable",
  outside_schedule: "Outside schedule",
  recording_disabled: "Recording disabled",
  worker_restart: "Recorder restarting",
  no_motion: "No motion",
  unknown: "Unknown",
};
