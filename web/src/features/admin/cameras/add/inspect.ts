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

/** An address as typed: a host name or IP address, optionally with `:port` for a camera whose ONVIF service is not on a usual port (IPv6 in brackets, `[::1]:8080`). */
export function splitAddress(text: string): { host: string; endpoint: string | null } {
  const trimmed = text.trim();
  const match = /^(?:\[([0-9A-Fa-f:.]+)\]|([^:\s[\]]+)):(\d{1,5})$/.exec(trimmed);
  const port = Number(match?.[3]);
  if (!match || port < 1 || port > 65535) return { host: trimmed.replace(/^\[([^\]]+)\]$/, "$1"), endpoint: null };
  const host = (match[1] ?? match[2]) as string;
  return { host, endpoint: `http://${host.includes(":") ? `[${host}]` : host}:${port}/onvif/device_service` };
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
  const typed = host.trim();  // With its port, so two cameras on one host are named apart
  return named ? `${named} ${typed}` : `Camera ${typed}`;
}

/** What is wrong, in a sentence, for a camera that was checked but not fully working. */
export function attentionText(report: InspectReport): string {
  const failed = report.steps.find(step => step.state === "failed" && step.id !== "ready");
  return failed ? `${failed.label}: ${failed.detail || "failed"}` : "Video could not be verified";
}

/** Save a checked camera. Recording starts only if asked for and everything checked out; a camera whose video could not be verified is saved anyway, with recording off, and rechecked by the NVR. */
export async function saveChecked(connection: QuickConnection, report: InspectReport, name: string, recording = false): Promise<Camera> {
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
      recording_mode: "motion", recording_enabled: recording && report.ok, save_anyway: !report.ok,
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

/** Split one line of comma-separated values. A field that starts with a double quote may hold commas and doubled quotes and is kept as written; a quote anywhere else is an ordinary character. Unquoted fields are trimmed. */
export function splitLine(line: string): string[] {
  const fields: string[] = [];
  let index = 0;
  while (index <= line.length) {
    let start = index;
    while (line[start] === " " || line[start] === "\t") start += 1;
    if (line[start] === '"') {
      let value = "";
      let cursor = start + 1;
      for (; cursor < line.length; cursor += 1) {
        if (line[cursor] === '"' && line[cursor + 1] === '"') { value += '"'; cursor += 1; } else if (line[cursor] === '"') break; else value += line[cursor];
      }
      fields.push(value);
      const comma = line.indexOf(",", cursor);
      if (comma < 0) break;
      index = comma + 1;
    } else {
      const comma = line.indexOf(",", index);
      fields.push((comma < 0 ? line.slice(index) : line.slice(index, comma)).trim());
      if (comma < 0) break;
      index = comma + 1;
    }
  }
  return fields;
}

export const BULK_LIMIT = 500;

/** Rows from lines of `address,username,password[,name]`. A header line, blank lines and `#` lines are skipped. */
export function parseBulk(text: string): BulkRow[] {
  const rows: BulkRow[] = [];
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
  lines.forEach((line, index) => {
    if (!line.trim() || line.trim().startsWith("#")) return;
    const [host = "", username = "", password = "", name = ""] = splitLine(line);
    if (rows.length === 0 && /^(address|host|ip|ip address|camera address)$/i.test(host)) return;
    let problem: string | null = null;
    if (!host) problem = "No address";
    else if (/\s/.test(host)) problem = "The address has a space in it";
    else if (!/^[A-Za-z0-9.\-:[\]]+$/.test(host) || !splitAddress(host).host) problem = "The address is not an IP address or host name";
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
