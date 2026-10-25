import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";

type Policy = "off" | "administrators" | "all";
interface LocalUser { id: number; username: string; role: string; local: boolean; totp: boolean; passkeys: number; recovery_codes_remaining: number; passkey_sign_in: boolean }

const POLICY_LABEL: Record<Policy, string> = {
  off: "Not required",
  administrators: "Required for administrators",
  all: "Required for all local accounts",
};

/** The second-factor policy, and who has factors set up, with a reset for someone who lost theirs. */
export function SignInSecurity() {
  const client = useQueryClient();
  const policy = useQuery({ queryKey: ["admin", "security"], queryFn: ({ signal }) => api<{ mfa_policy: Policy }>("/api/admin/security", { signal }) });
  const users = useQuery({ queryKey: ["admin", "users"], queryFn: ({ signal }) => api<{ users: LocalUser[] }>("/api/admin/users", { signal }).then(result => result.users) });
  const [resetting, setResetting] = useState<LocalUser | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: (value: Policy) => api<{ mfa_policy: Policy }>("/api/admin/security", { method: "PUT", body: { mfa_policy: value } }),
    onSuccess: result => { client.setQueryData(["admin", "security"], result); setNotice(`Second factor: ${POLICY_LABEL[result.mfa_policy].toLowerCase()}.`); setError(null); },
    onError: failure => setError(failureText(failure)),
  });
  const reset = useMutation({
    mutationFn: (user: LocalUser) => api<void>(`/api/admin/users/${user.id}/mfa`, { method: "DELETE" }),
    onSuccess: (_result, user) => { setNotice(`Second factors removed for ${user.username}; their sessions ended.`); setResetting(null); setError(null); void client.invalidateQueries({ queryKey: ["admin", "users"] }); },
    onError: failure => { setError(failureText(failure)); setResetting(null); },
  });

  return (
    <section className="field-group" aria-label="Sign-in security">
      <h2 className="field-heading">Sign-in security</h2>
      <label className="field"><span>Second factor for local accounts</span>
        <select value={policy.data?.mfa_policy ?? "off"} disabled={!policy.data || change.isPending} onChange={event => change.mutate(event.target.value as Policy)}>
          {(Object.keys(POLICY_LABEL) as Policy[]).map(value => <option key={value} value={value}>{POLICY_LABEL[value]}</option>)}
        </select>
      </label>
      <p className="muted field-hint">
        People with a passkey or authenticator app are always asked for it. Where it is required, someone without one sets one up before reaching the application, and you do too when you turn it on.
        Microsoft Entra sign-in is unchanged: its multi-factor authentication stays with your tenant. Failed sign-ins are slowed per account and per address, never locked for good.
      </p>
      {notice ? <p className="notice" role="status" data-tone="good">{notice}</p> : null}
      {error ? <p className="notice" role="alert" data-tone="bad">{error}</p> : null}
      {users.data?.length ? (
        <table className="inventory-table" aria-label="Accounts and second factors">
          <thead><tr><th scope="col">User</th><th scope="col">Role</th><th scope="col">Second factor</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            {users.data.map(user => {
              const factors = [user.totp ? "authenticator app" : null, user.passkeys ? `${user.passkeys} passkey${user.passkeys === 1 ? "" : "s"}` : null].filter(Boolean).join(", ");
              return (
                <tr key={user.id}>
                  <td>{user.username}</td>
                  <td>{user.role}</td>
                  <td>{!user.local ? "Managed by Entra" : factors || "None"}</td>
                  <td>{user.local && factors ? <button type="button" className="button" data-variant="ghost" aria-label={`Reset second factors for ${user.username}`} onClick={() => setResetting(user)}>Reset</button> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : <p className="muted">{users.isPending ? "Loading…" : "No accounts."}</p>}
      <Modal open={resetting !== null} onOpenChange={open => { if (!open) setResetting(null); }} title={`Reset second factors for ${resetting?.username ?? ""}?`}
             description="Removes their authenticator app, passkeys and recovery codes, and ends their sessions."
             footer={<><button type="button" className="button" onClick={() => setResetting(null)}>Cancel</button>
               <button type="button" className="button" data-variant="danger" disabled={reset.isPending} onClick={() => resetting && reset.mutate(resetting)}>Reset second factors</button></>}>
        <p className="muted">They sign in with their password and set up a new factor if the policy asks for one. The reset is recorded in the audit log.</p>
      </Modal>
    </section>
  );
}
