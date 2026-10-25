/** Alarm requests and queries: what rules saw, and how people handled it. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";

export type AlarmState = "new" | "acknowledged" | "assigned" | "resolved";
export type AlarmFilter = "open" | AlarmState | "all";

export interface Alarm {
  id: string;
  state: AlarmState;
  occurred_at: string;
  rule_id: string | null;
  rule_name: string;
  condition_type: string;
  camera_id: string;
  camera_name: string | null;
  track_id: string | null;
  label: "person" | "vehicle" | "covered" | "moved" | "defocused";
  assigned_to: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  updated_at: string;
}

export interface AlarmDetail extends Alarm {
  snapshot_url: string | null;
  clip: { starts_at: string; ends_at: string; segments: { id: number; starts_at: string; ends_at: string; media_url: string }[]; note: string | null };
  comments: { id: string; body: string; author: string | null; created_at: string }[];
  history: { action: string; actor: string | null; occurred_at: string; details: Record<string, unknown> }[];
}

export interface AlarmList { alarms: Alarm[]; counts: Record<AlarmState, number>; rules: { id: string; name: string }[] }
export interface AlarmQuery { state: AlarmFilter; camera_id: string; rule_id: string }

/** Other people's handling appears within this interval, without a reload. */
const ALARM_REFRESH_MS = 5_000;
export const alarmKeys = {
  all: ["alarms"] as const,
  list: (query: AlarmQuery) => ["alarms", "list", query] as const,
  detail: (id: string) => ["alarms", "detail", id] as const,
  assignees: ["alarm-assignees"] as const,
  settings: ["alarm-settings"] as const,
};

const at = (id: string) => `/api/alarms/${encodeURIComponent(id)}`;

export function useAlarms(query: AlarmQuery, enabled = true) {
  const params = new URLSearchParams({ state: query.state });
  if (query.camera_id) params.set("camera_id", query.camera_id);
  if (query.rule_id) params.set("rule_id", query.rule_id);
  return useQuery({
    queryKey: alarmKeys.list(query), enabled, refetchInterval: ALARM_REFRESH_MS,
    queryFn: ({ signal }) => api<AlarmList>(`/api/alarms?${params}`, { signal }),
  });
}

export function useAlarm(id: string | null) {
  return useQuery({
    queryKey: alarmKeys.detail(id ?? ""), enabled: Boolean(id), refetchInterval: ALARM_REFRESH_MS,
    queryFn: ({ signal }) => api<AlarmDetail>(at(id!), { signal }),
  });
}

/** The people an alarm of this camera can be assigned to: those who can see the camera. */
export function useAssignees(cameraId: string | undefined) {
  return useQuery({
    queryKey: [...alarmKeys.assignees, cameraId], enabled: cameraId !== undefined,
    queryFn: ({ signal }) => api<{ users: { id: number; username: string }[] }>(`/api/alarms/assignees?camera_id=${encodeURIComponent(cameraId ?? "")}`, { signal }).then(body => body.users),
  });
}

export type AlarmAction =
  | { kind: "acknowledge" }
  | { kind: "assign"; userId: number }
  | { kind: "comment"; body: string }
  | { kind: "resolve"; note: string }
  | { kind: "delete" };

export function useAlarmAction(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: AlarmAction): Promise<AlarmDetail | void> => {
      switch (action.kind) {
        case "acknowledge": return api<AlarmDetail>(`${at(id)}/acknowledge`, { method: "POST" });
        case "assign": return api<AlarmDetail>(`${at(id)}/assign`, { method: "POST", body: { user_id: action.userId } });
        case "comment": return api<AlarmDetail>(`${at(id)}/comments`, { method: "POST", body: { body: action.body } });
        case "resolve": return api<AlarmDetail>(`${at(id)}/resolve`, { method: "POST", body: { note: action.note || null } });
        case "delete": return api<void>(at(id), { method: "DELETE" });
      }
    },
    onSuccess: (result, action) => {
      if (action.kind === "delete") client.removeQueries({ queryKey: alarmKeys.detail(id) });
      else if (result) client.setQueryData(alarmKeys.detail(id), result);
      void client.invalidateQueries({ queryKey: [...alarmKeys.all, "list"] });
    },
  });
}

export const STATE_LABEL: Record<AlarmState, string> = { new: "New", acknowledged: "Acknowledged", assigned: "Assigned", resolved: "Resolved" };
export const FILTER_LABEL: Record<AlarmFilter, string> = { open: "Open", new: "New", acknowledged: "Acknowledged", assigned: "Assigned", resolved: "Resolved", all: "All" };
export const ACTION_LABEL: Record<string, string> = {
  created: "Raised", acknowledged: "Acknowledged", assigned: "Assigned", commented: "Commented", resolved: "Resolved",
};
