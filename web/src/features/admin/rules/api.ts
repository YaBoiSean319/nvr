/** Rule requests: administrator-defined conditions over detections, their events and test mode. */
import { api } from "@/api/client";
import type { Zone } from "@/features/admin/cameras/api";
import type { CrossingDirection, Line } from "@/features/admin/cameras/LineEditor";

export type ObjectClass = "person" | "vehicle";
export type TravelDirection = "right" | "left" | "down" | "up";

export type Condition =
  | { type: "zone_entry"; zone: Zone }
  | { type: "line_crossing"; line: Line; direction: CrossingDirection }
  | { type: "loitering"; zone: Zone; dwell_seconds: number }
  | { type: "wrong_way"; zone: Zone; direction: TravelDirection };

export interface RuleScheduleInterval { days: number[]; start: string; end: string }
export interface RuleSchedule { timezone: string; intervals: RuleScheduleInterval[] }

export interface RuleDefinition {
  name: string;
  enabled: boolean;
  camera_ids: string[];
  group_ids: string[];
  condition: Condition;
  classes: ObjectClass[];
  schedule: RuleSchedule | null;
  cooldown_seconds: number;
}

export interface Rule extends RuleDefinition {
  id: string;
  active_from: string;
  created_at: string;
  updated_at: string;
}

export interface WouldBeEvent { camera_id: string; camera_name: string; track_id: string; label: ObjectClass; occurred_at: string }
export interface TestResult { start: string; end: string; count: number; events: WouldBeEvent[]; coverage_note: string }

const at = (id: string) => `/api/rules/${encodeURIComponent(id)}`;

export const ruleKeys = { all: ["rules"] as const };
export const listRules = (signal?: AbortSignal) => api<{ rules: Rule[] }>("/api/rules", { signal }).then(body => body.rules);
export const createRule = (body: RuleDefinition) => api<Rule>("/api/rules", { method: "POST", body });
export const updateRule = (id: string, body: RuleDefinition) => api<Rule>(at(id), { method: "PUT", body });
export const deleteRule = (id: string) => api<void>(at(id), { method: "DELETE" });
export const testRule = (body: RuleDefinition) => api<TestResult>("/api/rules/test", { method: "POST", body });

export const CONDITION_LABEL: Record<Condition["type"], string> = {
  zone_entry: "Enters a zone",
  line_crossing: "Crosses a line",
  loitering: "Stays in a zone",
  wrong_way: "Moves the wrong way",
};

export const TRAVEL_LABEL: Record<TravelDirection, string> = { right: "Rightwards", left: "Leftwards", down: "Downwards", up: "Upwards" };

const FULL_FRAME: Zone = { x: 0.25, y: 0.25, width: 0.5, height: 0.5 };

/** A condition of another type that keeps whatever geometry the two share. */
export function switchCondition(current: Condition, type: Condition["type"]): Condition {
  const zone = "zone" in current ? current.zone : FULL_FRAME;
  if (type === "line_crossing") return { type, line: current.type === "line_crossing" ? current.line : { x1: 0.5, y1: 0.1, x2: 0.5, y2: 0.9 }, direction: "both" };
  if (type === "loitering") return { type, zone, dwell_seconds: current.type === "loitering" ? current.dwell_seconds : 30 };
  if (type === "wrong_way") return { type, zone, direction: current.type === "wrong_way" ? current.direction : "right" };
  return { type, zone };
}

export const newRule = (): RuleDefinition => ({
  name: "", enabled: true, camera_ids: [], group_ids: [], condition: { type: "zone_entry", zone: FULL_FRAME },
  classes: ["person"], schedule: null, cooldown_seconds: 0,
});

export const definitionOf = (rule: Rule): RuleDefinition => ({
  name: rule.name, enabled: rule.enabled, camera_ids: rule.camera_ids, group_ids: rule.group_ids, condition: rule.condition,
  classes: rule.classes, schedule: rule.schedule, cooldown_seconds: rule.cooldown_seconds,
});

/** What the form can tell before the server does. */
export function definitionProblem(definition: RuleDefinition): string | null {
  if (!definition.name.trim()) return "The rule needs a name.";
  if (!definition.camera_ids.length && !definition.group_ids.length) return "Choose at least one camera or group.";
  if (!definition.classes.length) return "Choose people, vehicles or both.";
  if (definition.schedule && !definition.schedule.intervals.length) return "Add a schedule interval, or make the rule always active.";
  return null;
}
