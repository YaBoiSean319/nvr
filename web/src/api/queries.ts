import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./client";
import { forgetThisDevice } from "@/features/alarms/pushDevice";
import type { Branding, MfaChallenge, Camera, CameraGroup, DisplaySettings, ExportJob, ExportRequest, Incident, LiveWorkspaceState, Me, PlaybackTimeline, RedactionEdit, RedactionReview, SavedView, ThemePreference } from "./types";

export const keys = {
  setup: ["setup"] as const,
  me: ["me"] as const,
  oidc: ["oidc"] as const,
  branding: ["branding"] as const,
  preferences: ["preferences"] as const,
  cameras: ["cameras"] as const,
  incidents: ["incidents", "open"] as const,
  exports: ["exports"] as const,
};

export function useSetupStatus() {
  return useQuery({
    queryKey: keys.setup,
    queryFn: ({ signal }) => api<{ setup_required: boolean }>("/api/setup/status", { signal, quietUnauthorized: true }),
    staleTime: Infinity,
  });
}

export function useMe(enabled = true) {
  return useQuery({
    queryKey: keys.me,
    enabled,
    queryFn: async ({ signal }) => {
      try {
        return await api<Me>("/api/auth/me", { signal, quietUnauthorized: true });
      } catch (error) {
        if (error instanceof ApiError && error.unauthorized) return null;
        throw error;
      }
    },
    staleTime: 60_000,
  });
}

export function useOidcStatus() {
  return useQuery({
    queryKey: keys.oidc,
    queryFn: ({ signal }) => api<{ enabled: boolean }>("/api/auth/oidc/status", { signal, quietUnauthorized: true }),
    staleTime: Infinity,
  });
}

export function usePublicBranding() {
  return useQuery({
    queryKey: keys.branding,
    queryFn: ({ signal }) => api<Branding>("/api/public/branding", { signal, quietUnauthorized: true }),
    staleTime: 5 * 60_000,
  });
}

export interface Preferences { theme: ThemePreference; live_alarm_pop_in: boolean }

export function usePreferences(enabled: boolean) {
  return useQuery({
    queryKey: keys.preferences,
    enabled,
    queryFn: ({ signal }) => api<Preferences>("/api/me/preferences", { signal }),
    staleTime: Infinity,
  });
}

export function useSavePreferences() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (changes: Partial<Preferences>) => api<Preferences>("/api/me/preferences", { method: "PUT", body: changes }),
    onSuccess: preferences => client.setQueryData(keys.preferences, preferences),
  });
}

export function useSignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
      api<Me | MfaChallenge>("/api/auth/login", { method: "POST", body: credentials, quietUnauthorized: true }),
    // A second factor still owed is not a session yet.
    onSuccess: result => (("mfa_required" in result) ? undefined : client.invalidateQueries()),
  });
}

export function useCreateAdministrator() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
      api<Me>("/api/setup/admin", { method: "POST", body: credentials, quietUnauthorized: true }),
    onSuccess: () => client.invalidateQueries(),
  });
}

export function useSignOut() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      // A shared device should stop getting the previous person's alarms; never let this block signing out.
      await forgetThisDevice().catch(() => undefined);
      return api<void>("/api/auth/logout", { method: "POST" });
    },
    onSettled: () => {
      // Drop everything that belonged to the session; keep what the sign-in screen needs.
      const keep = new Set<unknown>([keys.setup[0], keys.branding[0], keys.oidc[0], keys.me[0]]);
      client.removeQueries({ predicate: query => !keep.has(query.queryKey[0]) });
      client.setQueryData(keys.me, null);
    },
  });
}

const STATUS_REFRESH_MS = 15_000;

export function useCameras(enabled = true) {
  return useQuery({
    queryKey: keys.cameras,
    enabled,
    queryFn: ({ signal }) => api<{ cameras: Camera[] }>("/api/cameras", { signal }).then(result => result.cameras),
    refetchInterval: STATUS_REFRESH_MS,
  });
}

export function useOpenIncidents(enabled = true) {
  return useQuery({
    queryKey: keys.incidents,
    enabled,
    queryFn: async ({ signal }) => {
      const [active, acknowledged] = await Promise.all([
        api<{ incidents: Incident[] }>("/api/incidents?state=active", { signal }),
        api<{ incidents: Incident[] }>("/api/incidents?state=acknowledged", { signal }),
      ]);
      return [...active.incidents, ...acknowledged.incidents].sort((a, b) => b.last_seen_at.localeCompare(a.last_seen_at));
    },
    refetchInterval: STATUS_REFRESH_MS,
  });
}

export function useAcknowledgeIncident() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<Incident>(`/api/incidents/${encodeURIComponent(id)}/acknowledge`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.incidents }),
  });
}

export function useExportJobs(enabled = true) {
  return useQuery({
    queryKey: keys.exports,
    enabled,
    queryFn: ({ signal }) => api<{ exports: ExportJob[] }>("/api/exports", { signal }).then(result => result.exports),
    // Follow progress closely while work is queued or running.
    refetchInterval: query => (query.state.data?.some(job => job.status === "queued" || job.status === "running") ? 2_000 : STATUS_REFRESH_MS),
  });
}

function useExportAction<T>(run: (value: T) => Promise<ExportJob>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => client.invalidateQueries({ queryKey: keys.exports }) });
}

export const useCreateExport = () => useExportAction((request: ExportRequest) => api<ExportJob>("/api/exports", { method: "POST", body: request }));
export const useCancelExport = () => useExportAction((id: string) => api<ExportJob>(`/api/exports/${encodeURIComponent(id)}/cancel`, { method: "POST" }));
export const useRetryExport = () => useExportAction((id: string) => api<ExportJob>(`/api/exports/${encodeURIComponent(id)}/retry`, { method: "POST" }));

/** Which kinds of blur the installed detection models can find (none when no model is installed). */
export function useRedactionKinds(enabled = true) {
  return useQuery({
    queryKey: ["redaction", "models"] as const,
    enabled,
    queryFn: ({ signal }) => api<{ kinds: ("faces" | "plates")[] }>("/api/redaction/models", { signal }).then(result => result.kinds),
    staleTime: 60_000,
  });
}

export function useRedactionReview(id: string | null) {
  return useQuery({
    queryKey: ["redaction", "review", id] as const,
    enabled: id !== null,
    queryFn: ({ signal }) => api<RedactionReview>(`/api/redaction/reviews/${encodeURIComponent(id ?? "")}`, { signal }),
    refetchInterval: query => (query.state.data && (query.state.data.state === "queued" || query.state.data.state === "analysing") ? 1_500 : false),
  });
}

export function useStartRedactionReview() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: { camera_ids: string[]; start: string; end: string; kinds: ("faces" | "plates")[] }) =>
      api<RedactionReview>("/api/redaction/reviews", { method: "POST", body: request }),
    onSuccess: review => client.setQueryData(["redaction", "review", review.id], review),
  });
}

export function useChangeRedactionReview(id: string) {
  const client = useQueryClient();
  const store = (review: RedactionReview) => client.setQueryData(["redaction", "review", id], review);
  const path = `/api/redaction/reviews/${encodeURIComponent(id)}`;
  return {
    edit: useMutation({ mutationFn: (edit: RedactionEdit) => api<RedactionReview>(path, { method: "PATCH", body: edit }), onSuccess: store }),
    confirm: useMutation({ mutationFn: () => api<RedactionReview>(`${path}/confirm`, { method: "POST" }), onSuccess: store }),
  };
}

export const viewKeys = {
  views: ["live", "views"] as const,
  groups: ["camera-groups"] as const,
  workspace: ["live", "workspace"] as const,
};

export function useLiveViews() {
  return useQuery({
    queryKey: viewKeys.views,
    queryFn: async ({ signal }) => {
      const result = await api<{ layouts: SavedView[]; shared_views: SavedView[] }>("/api/live/views", { signal });
      return [...result.shared_views, ...result.layouts];
    },
  });
}

export function useCameraGroups() {
  return useQuery({
    queryKey: viewKeys.groups,
    queryFn: ({ signal }) => api<{ groups: CameraGroup[] }>("/api/camera-groups", { signal }).then(result => result.groups),
  });
}

export function useSaveView() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ name, scope, layout }: { name: string; scope: SavedView["scope"]; layout: Omit<SavedView, "name" | "scope"> }) =>
      api(`/api/live/${scope === "shared" ? "shared-layouts" : "layouts"}/${encodeURIComponent(name)}`, { method: "PUT", body: layout }),
    onSuccess: () => client.invalidateQueries({ queryKey: viewKeys.views }),
  });
}

export function useDeleteView() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ name, scope }: { name: string; scope: SavedView["scope"] }) =>
      api<void>(`/api/live/${scope === "shared" ? "shared-layouts" : "layouts"}/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: viewKeys.views }),
  });
}

export function fetchLiveWorkspace(signal?: AbortSignal) {
  return api<LiveWorkspaceState>("/api/live/workspace", { signal });
}

export function saveLiveWorkspace(state: Omit<LiveWorkspaceState, "camera_ids"> & { camera_ids: string[] }) {
  return api<LiveWorkspaceState>("/api/live/workspace", { method: "PUT", body: state });
}

export function useDisplaySettings() {
  return useQuery({
    queryKey: ["settings", "display"] as const,
    queryFn: ({ signal }) => api<DisplaySettings>("/api/settings/display", { signal }),
    staleTime: 60_000,
  });
}

/**
 * The aligned timeline for a search. Changing the cameras keeps showing the previous
 * result until the new one arrives, so adding a camera never blanks the investigation.
 */
export function usePlaybackTimeline(cameraIds: string[], range: { start: string; end: string } | null) {
  return useQuery({
    queryKey: ["playback", "timeline", range?.start, range?.end, cameraIds] as const,
    enabled: Boolean(range && cameraIds.length),
    queryFn: ({ signal }) => api<PlaybackTimeline>("/api/playback/timeline", {
      method: "POST", signal, body: { camera_ids: cameraIds, start: range!.start, end: range!.end },
    }),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}
