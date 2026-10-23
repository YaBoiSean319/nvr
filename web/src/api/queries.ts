import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./client";
import type { Branding, Camera, ExportJob, Incident, Me, ThemePreference } from "./types";

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

export function usePreferences(enabled: boolean) {
  return useQuery({
    queryKey: keys.preferences,
    enabled,
    queryFn: ({ signal }) => api<{ theme: ThemePreference }>("/api/me/preferences", { signal }),
    staleTime: Infinity,
  });
}

export function useSavePreferences() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (theme: ThemePreference) => api<{ theme: ThemePreference }>("/api/me/preferences", { method: "PUT", body: { theme } }),
    onSuccess: preferences => client.setQueryData(keys.preferences, preferences),
  });
}

export function useSignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
      api<Me>("/api/auth/login", { method: "POST", body: credentials, quietUnauthorized: true }),
    onSuccess: () => client.invalidateQueries(),
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
    mutationFn: () => api<void>("/api/auth/logout", { method: "POST" }),
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
    refetchInterval: STATUS_REFRESH_MS,
  });
}
