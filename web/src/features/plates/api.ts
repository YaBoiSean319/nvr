/** Licence plates: the cameras that read them, watch lists and their entries (administrators), and the plate search. */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";

export interface WatchEntry { id: string; pattern: string; note: string; expires_at: string | null; expired: boolean; created_at: string }
export interface WatchList { id: string; name: string; kind: "allow" | "deny"; lookalikes: boolean; created_at: string; entries: WatchEntry[] }
export interface PlateCamera { id: string; name: string; enabled: boolean; min_confidence: number }
export interface PlateAdmin {
  models: { available: boolean; detector: string; recogniser: string; detector_installed: boolean; recogniser_installed: boolean };
  cameras: PlateCamera[];
  watch_lists: WatchList[];
}
export interface PlateRead {
  id: string; camera_id: string; camera_name: string; track_id: string; plate: string; confidence: number; read_at: string;
  box: { x: number; y: number; width: number; height: number }; has_crop: boolean;
}
export interface PlateSearchResponse { query: string; pattern: string; lookalikes: boolean; results: PlateRead[]; truncated: boolean; cameras_reading: string[]; note: string }

export const PLATES_KEY = ["admin", "plates"] as const;
export const KIND_LABEL: Record<WatchList["kind"], string> = { deny: "Deny list: raise an alarm for plates on it", allow: "Allow list: raise an alarm for plates not on it" };

export const usePlateAdmin = () => useQuery({ queryKey: PLATES_KEY, queryFn: ({ signal }) => api<PlateAdmin>("/api/admin/plates", { signal }) });
