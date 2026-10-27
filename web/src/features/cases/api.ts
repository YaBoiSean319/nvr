import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

export type ItemKind = "clip" | "mark" | "note" | "alarm" | "search" | "export";
export interface CaseCounts { clip: number; mark: number; note: number; alarm: number; search: number; export: number }
export interface CaseSummary { id: string; title: string; status: "open" | "closed"; owner: string | null; owner_id: number | null; created_at: string; closed_at: string | null; counts: CaseCounts }
export interface CaseItem {
  id: string; kind: ItemKind; restricted: boolean; added_at: string; camera_id?: string | null; camera_name?: string | null; starts_at?: string | null; ends_at?: string | null;
  ref?: string | null; text?: string; data?: Record<string, unknown>; added_by?: string | null;
}
export interface CasePackage { id: string; status: "building" | "ready" | "failed"; error: string | null; omitted_items: number; created_at: string; completed_at: string | null; size_bytes: number | null; sha256: string | null }
export interface CaseDetail extends CaseSummary {
  description: string; items: CaseItem[]; restricted_items: number; held_segments: number; can_manage: boolean; packages: CasePackage[];
  members: { user_id: number; username: string; added_at: string; owner: boolean }[];
}
export interface CaseEvent { at: string; actor: string | null; action: string; detail: Record<string, unknown> }
export interface PackageCheck {
  checksums_valid: boolean; clips_valid?: boolean; verified: boolean; matches_issued_package: boolean; clips_match_request: boolean; complete: boolean; omitted_items?: number; notes_and_audit_present?: boolean; error: string | null;
  clips: { item_id: string | null; export_id: string | null; present: boolean; valid: boolean; error?: string | null }[];
}

export const caseKeys = { all: ["cases"] as const, one: (id: string) => ["cases", id] as const, audit: (id: string) => ["cases", id, "audit"] as const };

export function useCases() {
  return useQuery({ queryKey: caseKeys.all, queryFn: ({ signal }) => api<{ cases: CaseSummary[] }>("/api/cases", { signal }).then(result => result.cases) });
}

export function useCase(id: string) {
  return useQuery({
    queryKey: caseKeys.one(id),
    queryFn: ({ signal }) => api<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`, { signal }),
    // A package is built in the background: look again until it is done.
    refetchInterval: query => (query.state.data?.packages.some(item => item.status === "building") ? 3000 : false),
  });
}

export function useCaseAudit(id: string, enabled: boolean) {
  return useQuery({ queryKey: caseKeys.audit(id), enabled, queryFn: ({ signal }) => api<{ events: CaseEvent[] }>(`/api/cases/${encodeURIComponent(id)}/audit`, { signal }).then(result => result.events) });
}

export function createCase(title: string, description: string) {
  return api<CaseDetail>("/api/cases", { method: "POST", body: { title, description } });
}

export function addItem(id: string, body: Record<string, unknown>) {
  return api<{ item_id: string; case: CaseDetail }>(`/api/cases/${encodeURIComponent(id)}/items`, { method: "POST", body });
}
