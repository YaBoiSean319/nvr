/** Inventory filtering, sorting and honest status labels, independent of React. */
import type { Camera, CameraBrand } from "@/api/types";

export type Connection = "connected" | "unreachable" | "not_monitored" | "unknown";
export type SortKey = "name" | "ip" | "group" | "connection" | "health" | "mode" | "brand";

export interface Filters {
  query: string;
  archive: "active" | "archived" | "all";
  group: string; // "all", "__none__" or a group name
  connection: Connection | "all";
  health: Camera["recording_health"]["state"] | "all";
}

export interface Sort {
  key: SortKey;
  direction: 1 | -1;
}

export const DEFAULT_FILTERS: Filters = { query: "", archive: "active", group: "all", connection: "all", health: "all" };
export const DEFAULT_SORT: Sort = { key: "name", direction: 1 };

/**
 * Whether the NVR is currently receiving the camera's stream. The recorder only connects to
 * cameras it records, so an unrecorded camera is "not monitored" rather than guessed healthy.
 */
export function connectionOf(camera: Camera): Connection {
  if (camera.archived_at || !camera.recording_enabled) return "not_monitored";
  if (camera.recording_health.state === "camera_unreachable") return "unreachable";
  if (camera.recording_health.video_fresh) return "connected";
  return "unknown";
}

export const CONNECTION_LABEL: Record<Connection, string> = {
  connected: "Connected",
  unreachable: "Unreachable",
  not_monitored: "Not monitored",
  unknown: "Not reported",
};

export const HEALTH_LABEL: Record<Camera["recording_health"]["state"], string> = {
  recording: "Recording",
  expected_idle: "Idle (expected)",
  uncertain: "Uncertain",
  motion_source_degraded: "Motion source degraded",
  camera_unreachable: "Camera unreachable",
  write_stalled: "Writes stalled",
};

export const BRAND_LABEL: Record<CameraBrand, string> = {
  auto: "Unknown",
  acti: "ACTi",
  axis: "Axis",
  bosch: "Bosch",
  dahua: "Dahua",
  hanwha: "Hanwha",
  hikvision: "Hikvision",
  reolink: "Reolink",
  samsung: "Samsung",
  uniview: "Uniview",
  other: "Other",
};

export function modeOf(camera: Camera): Camera["recording_mode"] {
  return camera.effective_recording_mode ?? camera.recording_mode;
}

export const MODE_LABEL: Record<Camera["recording_mode"], string> = { motion: "Motion", continuous: "Continuous", disabled: "Disabled" };

function sortValue(camera: Camera, key: SortKey): string {
  switch (key) {
    case "name": return camera.name;
    case "ip": return camera.camera_ip ?? "";
    case "group": return camera.group_name ?? "";
    case "connection": return CONNECTION_LABEL[connectionOf(camera)];
    case "health": return HEALTH_LABEL[camera.recording_health.state];
    case "mode": return MODE_LABEL[modeOf(camera)];
    case "brand": return BRAND_LABEL[camera.camera_brand ?? "auto"];
  }
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export function matches(camera: Camera, filters: Filters): boolean {
  if (filters.archive === "active" && camera.archived_at) return false;
  if (filters.archive === "archived" && !camera.archived_at) return false;
  const query = filters.query.trim().toLocaleLowerCase();
  if (query && !`${camera.name} ${camera.camera_ip ?? ""}`.toLocaleLowerCase().includes(query)) return false;
  if (filters.group === "__none__" && camera.group_name) return false;
  if (filters.group !== "all" && filters.group !== "__none__" && camera.group_name !== filters.group) return false;
  if (filters.connection !== "all" && connectionOf(camera) !== filters.connection) return false;
  if (filters.health !== "all" && camera.recording_health.state !== filters.health) return false;
  return true;
}

/** Filtered and sorted; ties fall back to the stable camera ID so order never jumps. */
export function visibleCameras(cameras: Camera[], filters: Filters, sort: Sort): Camera[] {
  return cameras
    .filter(camera => matches(camera, filters))
    .sort((a, b) => collator.compare(sortValue(a, sort.key), sortValue(b, sort.key)) * sort.direction || collator.compare(a.id, b.id));
}

/** The smallest change to the filters that shows this camera again. */
export function revealing(camera: Camera, filters: Filters): Filters {
  const next = { ...filters };
  if (!matches(camera, { ...DEFAULT_FILTERS, archive: "all", query: filters.query })) next.query = "";
  if (filters.archive === "active" && camera.archived_at) next.archive = "all";
  if (filters.archive === "archived" && !camera.archived_at) next.archive = "all";
  if (!matches(camera, { ...DEFAULT_FILTERS, archive: "all", group: filters.group })) next.group = "all";
  if (!matches(camera, { ...DEFAULT_FILTERS, archive: "all", connection: filters.connection })) next.connection = "all";
  if (!matches(camera, { ...DEFAULT_FILTERS, archive: "all", health: filters.health })) next.health = "all";
  return next;
}
