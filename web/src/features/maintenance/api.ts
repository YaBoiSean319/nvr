import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";

export interface MaintenanceWindow {
  id: string;
  name: string;
  reason: string;
  scope: "cameras" | "groups" | "site";
  state: "upcoming" | "active" | "ended";
  camera_ids: string[];
  camera_names: string[];
  group_ids: string[];
  group_names: string[];
  starts_at: string;
  ends_at: string;
  ended_at: string | null;
  created_by: string | null;
  suppressed_alarms: number;
  suppressed_incidents: number;
}

export interface MaintenanceList {
  now: string;
  windows: MaintenanceWindow[];
  site_active: boolean;
  covered_camera_ids: string[];
  limits: { operator_hours: number; administrator_days: number };
}

export interface WindowRequest {
  name: string;
  reason: string;
  scope: "cameras" | "groups" | "site";
  camera_ids: string[];
  group_ids: string[];
  starts_at?: string;
  duration_minutes: number;
}

export const MAINTENANCE_KEY = ["maintenance"] as const;

export const useMaintenance = () =>
  useQuery({ queryKey: MAINTENANCE_KEY, queryFn: ({ signal }) => api<MaintenanceList>("/api/maintenance", { signal }), refetchInterval: 30_000 });

export function useOpenWindow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: WindowRequest) => api<MaintenanceWindow>("/api/maintenance", { method: "POST", body: request }),
    onSuccess: () => client.invalidateQueries({ queryKey: MAINTENANCE_KEY }),
  });
}

export function useEndWindow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/maintenance/${encodeURIComponent(id)}/end`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: MAINTENANCE_KEY }),
  });
}
