/** Rule requests: administrator-defined conditions over detections, their events and test mode. */
import { api } from "@/api/client";
import type { Zone } from "@/features/admin/cameras/api";
import type { CrossingDirection, Line } from "@/features/admin/cameras/LineEditor";

export type ObjectClass = "person" | "vehicle";
export type TravelDirection = "right" | "left" | "down" | "up";
export type TamperKind = "covered" | "moved" | "defocused";
export const TAMPER_KINDS: TamperKind[] = ["covered", "moved", "defocused"];
export const TAMPER_LABEL: Record<TamperKind, string> = { covered: "Covered", moved: "Moved", defocused: "Defocused" };

export type Condition =
  | { type: "zone_entry"; zone: Zone }
  | { type: "line_crossing"; line: Line; direction: CrossingDirection }
  | { type: "loitering"; zone: Zone; dwell_seconds: number }
  | { type: "wrong_way"; zone: Zone; direction: TravelDirection }
  | { type: "tamper"; kinds: TamperKind[] }
  | { type: "external_event"; source_ids: string[]; types: string[]; subjects: string[] }
  | { type: "plate"; watch_list_id: string };

export interface RuleScheduleInterval { days: number[]; start: string; end: string }
export interface RuleSchedule { timezone: string; intervals: RuleScheduleInterval[] }

export type ActionType = "webhook" | "mqtt" | "slack" | "teams" | "relay";

/** One rule action as the form edits it. `url` and `signing_secret` are write-only: left empty on a saved action, the saved ones are kept. */
export interface ActionDraft {
  id?: string;
  type: ActionType;
  name: string;
  enabled: boolean;
  url?: string;
  signing_secret?: string;
  allow_private?: boolean;
  topic?: string;
  retain?: boolean;
  camera_id?: string | null;
  relay_token?: string | null;
  duration_seconds?: number;
  /** From the server: where a saved action delivers, without its secret parts. */
  target?: string | null;
  has_signing_secret?: boolean;
  /** Identifies an unsaved action in the form; the server ignores it. */
  key?: string;
}

export interface RuleDefinition {
  name: string;
  enabled: boolean;
  camera_ids: string[];
  group_ids: string[];
  condition: Condition;
  classes: ObjectClass[];
  schedule: RuleSchedule | null;
  cooldown_seconds: number;
  pop_into_live: boolean;
  actions: ActionDraft[];
}

export interface Rule extends RuleDefinition {
  id: string;
  active_from: string;
  created_at: string;
  updated_at: string;
}

export interface WouldBeEvent { camera_id: string; camera_name: string; track_id: string | null; label: ObjectClass | TamperKind; occurred_at: string }
export interface TestResult { start: string; end: string; count: number; events: WouldBeEvent[]; coverage_note: string; actions: { type: ActionType; name: string; target: string | null }[] }

export interface ActionDelivery {
  id: string; action_id: string | null; action_name: string; action_type: ActionType; rule_event_id: string; alarm_id: string | null;
  status: "queued" | "sending" | "delivered" | "failed"; attempt_count: number; next_attempt_at: string; response_status: number | null;
  last_error: string | null; created_at: string; updated_at: string; delivered_at: string | null;
}

const at = (id: string) => `/api/rules/${encodeURIComponent(id)}`;

export const ruleKeys = { all: ["rules"] as const };
export const listRules = (signal?: AbortSignal) => api<{ rules: Rule[] }>("/api/rules", { signal }).then(body => body.rules);
export const createRule = (body: RuleDefinition) => api<Rule>("/api/rules", { method: "POST", body });
export const updateRule = (id: string, body: RuleDefinition) => api<Rule>(at(id), { method: "PUT", body });
export const deleteRule = (id: string) => api<void>(at(id), { method: "DELETE" });
export const testRule = (body: RuleDefinition) => api<TestResult>("/api/rules/test", { method: "POST", body });
export const listDeliveries = (id: string, signal?: AbortSignal) =>
  api<{ deliveries: ActionDelivery[] }>(`${at(id)}/deliveries`, { signal }).then(body => body.deliveries);

export const ACTION_LABEL: Record<ActionType, string> = {
  webhook: "Signed webhook", mqtt: "MQTT message", slack: "Slack message", teams: "Microsoft Teams message", relay: "Camera relay output",
};

export function newAction(type: ActionType): ActionDraft {
  const base = { type, name: ACTION_LABEL[type], enabled: true, key: `new-${Date.now()}-${Math.random().toString(36).slice(2)}` };
  if (type === "mqtt") return { ...base, url: "", topic: "", retain: false, allow_private: false };
  if (type === "relay") return { ...base, camera_id: null, relay_token: null, duration_seconds: 1 };
  if (type === "webhook") return { ...base, url: "", signing_secret: "", allow_private: false };
  return { ...base, url: "" };
}

export const CONDITION_LABEL: Record<Condition["type"], string> = {
  zone_entry: "Enters a zone",
  line_crossing: "Crosses a line",
  loitering: "Stays in a zone",
  wrong_way: "Moves the wrong way",
  tamper: "Camera is covered, moved or defocused",
  external_event: "An outside event (door, sensor, button)",
  plate: "A licence plate is read (watch list)",
};

export const TRAVEL_LABEL: Record<TravelDirection, string> = { right: "Rightwards", left: "Leftwards", down: "Downwards", up: "Upwards" };

const DEFAULT_ZONE: Zone = { x: 0.25, y: 0.25, width: 0.5, height: 0.5 };

/** A condition of another type that keeps whatever geometry the two share. */
export function switchCondition(current: Condition, type: Condition["type"]): Condition {
  if (type === "external_event") return { type, source_ids: [], types: [], subjects: [] };
  if (type === "plate") return { type, watch_list_id: current.type === "plate" ? current.watch_list_id : "" };
  if (type === "tamper") return { type, kinds: current.type === "tamper" ? current.kinds : [...TAMPER_KINDS] };
  const zone = "zone" in current ? current.zone : DEFAULT_ZONE;
  if (type === "line_crossing") return { type, line: current.type === "line_crossing" ? current.line : { x1: 0.5, y1: 0.1, x2: 0.5, y2: 0.9 }, direction: "both" };
  if (type === "loitering") return { type, zone, dwell_seconds: current.type === "loitering" ? current.dwell_seconds : 30 };
  if (type === "wrong_way") return { type, zone, direction: current.type === "wrong_way" ? current.direction : "right" };
  return { type, zone };
}

export const newRule = (): RuleDefinition => ({
  name: "", enabled: true, camera_ids: [], group_ids: [], condition: { type: "zone_entry", zone: DEFAULT_ZONE },
  classes: ["person"], schedule: null, cooldown_seconds: 0, pop_into_live: false, actions: [],
});

export const definitionOf = (rule: Rule): RuleDefinition => ({
  name: rule.name, enabled: rule.enabled, camera_ids: rule.camera_ids, group_ids: rule.group_ids, condition: rule.condition,
  classes: rule.classes, schedule: rule.schedule, cooldown_seconds: rule.cooldown_seconds, pop_into_live: rule.pop_into_live,
  // Saved secrets never come back; the form leaves them empty to keep them.
  actions: rule.actions.map(action => ({ ...action })),
});

/** What the form can tell before the server does. */
export function definitionProblem(definition: RuleDefinition): string | null {
  if (!definition.name.trim()) return "The rule needs a name.";
  if (!definition.camera_ids.length && !definition.group_ids.length) return "Choose at least one camera or group.";
  if (definition.condition.type === "tamper") {
    if (!definition.condition.kinds.length) return "Choose at least one kind of tampering.";
  } else if (definition.condition.type === "external_event") {
    // Objects are not part of an outside event.
  } else if (definition.condition.type === "plate") {
    if (!definition.condition.watch_list_id) return "Choose the watch list the rule uses.";
  } else if (!definition.classes.length) return "Choose people, vehicles or both.";
  if (definition.condition.type === "loitering" && !(definition.condition.dwell_seconds >= 1)) return "Enter how many seconds an object must stay.";
  if (!(definition.cooldown_seconds >= 0)) return "Enter a cooldown of 0 seconds or more.";
  if (definition.schedule && !definition.schedule.intervals.length) return "Add a schedule interval, or make the rule always active.";
  for (const action of definition.actions) {
    if (!action.name.trim()) return "Every action needs a name.";
    if (action.type === "relay" && !action.camera_id) return `Choose the camera whose relay ${action.name} switches.`;
    if (action.type === "relay" && !((action.duration_seconds ?? 0) >= 0.1 && (action.duration_seconds ?? 0) <= 60)) return `Hold ${action.name}'s relay for 0.1 to 60 seconds.`;
    if (action.type !== "relay" && !action.id && !action.url?.trim()) return `Enter the destination for ${action.name}.`;
    if (action.type === "webhook" && !action.has_signing_secret && (action.signing_secret ?? "").length < 16) return `Enter a signing secret of at least 16 characters for ${action.name}.`;
    if (action.type === "mqtt" && !action.topic?.trim()) return `Enter the MQTT topic for ${action.name}.`;
  }
  return null;
}
