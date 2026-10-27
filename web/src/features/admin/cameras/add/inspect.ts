import type { Camera, CameraBrand, InspectReport } from "@/api/types";
import { api } from "@/api/client";
import { failureText } from "../../tabDraft";

/** The login and address entered for a camera. It lives only in the form that holds it, never in storage. */
export interface QuickConnection { host: string; username: string; password: string; endpoint: string; brand: CameraBrand }

export const EMPTY_QUICK: QuickConnection = { host: "", username: "", password: "", endpoint: "", brand: "auto" };

export const CAPABILITY_LABEL: Record<string, string> = {
  discovery: "Camera found", authentication: "Login", video: "Video", audio: "Audio", motion: "Motion events", ptz: "PTZ",
};

export const STATE_TEXT: Record<string, string> = {
  ok: "Working", available: "Available", unsupported: "Not offered", failed: "Needs attention", unknown: "Not checked", pending: "Waiting", skipped: "Skipped",
};

/** An address as typed: a host name or IP address, optionally with `:port` for a camera whose ONVIF service is not on a usual port (not for IPv6). */
export function splitAddress(text: string): { host: string; endpoint: string | null } {
  const trimmed = text.trim();
  const match = /^([^:\s]+):(\d{1,5})$/.exec(trimmed);
  if (!match) return { host: trimmed, endpoint: null };
  return { host: match[1] as string, endpoint: `http://${match[1]}:${match[2]}/onvif/device_service` };
}

export function inspectCamera(connection: QuickConnection, verifyVideo = true): Promise<InspectReport> {
  const value = (text: string) => text.trim() || null;
  const address = splitAddress(connection.host);
  return api<InspectReport>("/api/cameras/inspect", {
    method: "POST",
    body: {
      host: address.host, username: value(connection.username), password: connection.password || null,
      device_service_url: value(connection.endpoint) ?? address.endpoint, camera_brand: connection.brand, verify_video: verifyVideo,
    },
  });
}

export function suggestedName(report: InspectReport | null, host: string): string {
  const named = [report?.device.manufacturer, report?.device.model].filter(Boolean).join(" ").trim();
  const bare = splitAddress(host).host;
  return named ? `${named} ${bare}` : `Camera ${bare}`;
}

/** What is wrong, in a sentence, for a camera that was checked but not fully working. */
export function attentionText(report: InspectReport): string {
  const failed = report.steps.find(step => step.state === "failed" && step.id !== "ready");
  return failed ? `${failed.label}: ${failed.detail || "failed"}` : "Video could not be verified";
}

/** Save a checked camera. A camera whose video could not be verified is saved anyway, with recording off, and rechecked by the NVR. */
export async function saveChecked(connection: QuickConnection, report: InspectReport, name: string): Promise<Camera> {
  const value = (text: string) => text.trim() || null;
  const selection = report.selection;
  const address = splitAddress(connection.host);
  return api<Camera>("/api/cameras", {
    method: "POST",
    body: {
      name: name.trim(), camera_ip: address.host, camera_username: value(connection.username), camera_password: connection.password || null,
      camera_brand: report.brand && report.brand !== "auto" ? report.brand : connection.brand,
      device_service_url: report.device_service_url ?? value(connection.endpoint) ?? address.endpoint,
      onvif_profile_token: selection?.record_token ?? null,
      live_substream_profile_token: selection?.live_token && selection.live_verified !== false ? selection.live_token : null,
      recording_mode: "motion", recording_enabled: false, save_anyway: !report.ok,
    },
  });
}

export { failureText };

/** One line of a bulk list. */
export interface BulkRow {
  row: number;
  host: string;
  username: string;
  password: string;
  name: string;
  problem: string | null;
}

/** Split one line of comma-separated values; a field in double quotes may hold commas and doubled quotes. */
export function splitLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (quoted) {
      if (char === '"' && line[index + 1] === '"') { current += '"'; index += 1; } else if (char === '"') quoted = false; else current += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { fields.push(current); current = ""; } else current += char;
  }
  fields.push(current);
  return fields.map(field => field.trim());
}

export const BULK_LIMIT = 500;

/** Rows from lines of `address,username,password[,name]`. A header line, blank lines and `#` lines are skipped. */
export function parseBulk(text: string): BulkRow[] {
  const rows: BulkRow[] = [];
  const lines = text.split(/\r?\n/);
  lines.forEach((line, index) => {
    if (!line.trim() || line.trim().startsWith("#")) return;
    const [host = "", username = "", password = "", name = ""] = splitLine(line);
    if (rows.length === 0 && /^(address|host|ip|ip address|camera address)$/i.test(host)) return;
    let problem: string | null = null;
    if (!host) problem = "No address";
    else if (/\s/.test(host)) problem = "The address has a space in it";
    else if (splitAddress(host).host.includes(":") || !/^[A-Za-z0-9.\-:[\]]+$/.test(host)) problem = "The address is not an IP address or host name";
    else if (Boolean(username) !== Boolean(password)) problem = "Give both a username and a password, or neither";
    rows.push({ row: index + 1, host, username, password, name: name.slice(0, 120), problem });
  });
  return rows;
}

export type BulkStatus = "queued" | "checking" | "adding" | "added" | "attention" | "failed" | "invalid" | "skipped";

export const BULK_LABEL: Record<BulkStatus, string> = {
  queued: "Waiting", checking: "Checking…", adding: "Adding…", added: "Added", attention: "Added, needs attention", failed: "Not added", invalid: "Invalid", skipped: "Not run",
};

/** Run `work` over `items`, never more than `limit` at once, so a long list does not open a connection to every camera together. */
export async function runLimited<T>(items: T[], limit: number, work: (item: T) => Promise<void>, stopped: () => boolean): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (!stopped()) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      await work(items[index] as T);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}
