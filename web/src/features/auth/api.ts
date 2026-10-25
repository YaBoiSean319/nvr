import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { Me, SecuritySummary } from "@/api/types";
import { createPasskey, getPasskey, type CreationOptionsJson, type CredentialJson, type RequestOptionsJson } from "./webauthn";

const signedIn = (client: ReturnType<typeof useQueryClient>) => () => client.invalidateQueries();

/** Answer a sign-in's challenge with an authenticator code or a recovery code. */
export function useSecondFactor() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { challenge: string; method: "totp" | "recovery"; code: string }) =>
      api<Me>("/api/auth/mfa/verify", { method: "POST", body: input, quietUnauthorized: true }),
    onSuccess: signedIn(client),
  });
}

/** Answer a sign-in's challenge with a passkey. */
export function usePasskeySecondFactor() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (challenge: string) => {
      const options = await api<RequestOptionsJson>("/api/auth/mfa/passkey/options", { method: "POST", body: { challenge }, quietUnauthorized: true });
      const credential = await getPasskey(options);
      return api<Me>("/api/auth/mfa/passkey/verify", { method: "POST", body: { challenge, credential }, quietUnauthorized: true });
    },
    onSuccess: signedIn(client),
  });
}

/** Sign in with only a passkey, for people who turned that on. */
export function usePasskeySignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const started = await api<{ challenge: string; options: RequestOptionsJson }>("/api/auth/passkey/options", { method: "POST", quietUnauthorized: true });
      const credential = await getPasskey(started.options);
      return api<Me>("/api/auth/passkey/login", { method: "POST", body: { challenge: started.challenge, credential }, quietUnauthorized: true });
    },
    onSuccess: signedIn(client),
  });
}

const SECURITY = ["me", "security"] as const;

export function useSecuritySummary() {
  return useQuery({ queryKey: SECURITY, queryFn: ({ signal }) => api<SecuritySummary>("/api/me/security", { signal }) });
}

/** What changed a person's own factors: the new summary, and recovery codes when there are new ones. */
export type SecurityResult = SecuritySummary & { recovery_codes?: string[] | null };

export function useSecurityActions() {
  const client = useQueryClient();
  const done = (result: SecurityResult) => {
    const { recovery_codes: _codes, ...summary } = result;
    client.setQueryData(SECURITY, summary);
    return result;
  };
  return {
    // The password is asked for only when adding to an account that already has a factor.
    beginTotp: useMutation({ mutationFn: (password?: string) => api<{ secret: string; otpauth_uri: string }>("/api/me/security/totp/begin", { method: "POST", body: password ? { password } : undefined }) }),
    confirmTotp: useMutation({ mutationFn: (code: string) => api<SecurityResult>("/api/me/security/totp/confirm", { method: "POST", body: { code } }).then(done) }),
    removeTotp: useMutation({ mutationFn: (password: string) => api<SecurityResult>("/api/me/security/totp", { method: "DELETE", body: { password } }).then(done) }),
    addPasskey: useMutation({
      mutationFn: async (input: { name: string; password?: string }) => {
        const options = await api<CreationOptionsJson>("/api/me/security/passkeys/options", { method: "POST", body: input.password ? { password: input.password } : undefined });
        const credential: CredentialJson = await createPasskey(options);
        return api<SecurityResult>("/api/me/security/passkeys", { method: "POST", body: { credential, name: input.name } }).then(done);
      },
    }),
    removePasskey: useMutation({ mutationFn: (input: { id: number; password: string }) => api<SecurityResult>(`/api/me/security/passkeys/${input.id}`, { method: "DELETE", body: { password: input.password } }).then(done) }),
    setPasskeySignIn: useMutation({ mutationFn: (input: { enabled: boolean; password: string }) => api<SecurityResult>("/api/me/security/passkey-sign-in", { method: "PUT", body: input }).then(done) }),
    regenerateCodes: useMutation({ mutationFn: (password: string) => api<SecurityResult>("/api/me/security/recovery-codes", { method: "POST", body: { password } }).then(done) }),
  };
}
