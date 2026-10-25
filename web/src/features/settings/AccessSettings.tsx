import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/api/client";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";
import { SecretDialog } from "./SecretDialog";
import { CameraAccess } from "./CameraAccess";
import { SignInSecurity } from "./SignInSecurity";

interface Session { session_id: string; created_at: string; expires_at: string; username: string; role: string }
interface Token {
  id: string; name: string; scopes: string[]; state: "active" | "revoked" | "expired"; created_by: string | null;
  created_at: string; expires_at: string | null; revoked_at: string | null; last_used_at: string | null;
}

const SCOPE_HINT: Record<string, string> = {
  read: "Read status, cameras and timelines",
  media: "Watch live video and recorded footage",
  export: "Create and download exports",
  admin: "Change configuration (acts as the administrator who created it)",
};

/** Sign-in options, active sessions and scoped API tokens. */
export function AccessSettings() {
  const client = useQueryClient();
  const sso = useQuery({ queryKey: ["oidc"], queryFn: ({ signal }) => api<{ enabled: boolean }>("/api/auth/oidc/status", { signal, quietUnauthorized: true }) });
  const sessions = useQuery({ queryKey: ["admin", "sessions"], queryFn: ({ signal }) => api<{ sessions: Session[] }>("/api/admin/sessions", { signal }).then(result => result.sessions) });
  const tokens = useQuery({ queryKey: ["admin", "api-tokens"], queryFn: ({ signal }) => api<{ tokens: Token[]; scopes: string[] }>("/api/admin/api-tokens", { signal }) });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [expires, setExpires] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<Token | null>(null);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const result = await api<{ token: Token; secret: string }>("/api/admin/api-tokens", {
        method: "POST", body: { name: name.trim(), scopes, expires_at: expires ? new Date(expires).toISOString() : null },
      });
      setSecret(result.secret);
      setName("");
      setExpires("");
      await client.invalidateQueries({ queryKey: ["admin", "api-tokens"] });
    } catch (failure) {
      setError(failureText(failure));
    }
  }

  async function revoke(token: Token) {
    try {
      await api(`/api/admin/api-tokens/${encodeURIComponent(token.id)}`, { method: "DELETE" });
      setRevoking(null);
      await client.invalidateQueries({ queryKey: ["admin", "api-tokens"] });
    } catch (failure) {
      setError(failureText(failure));
      setRevoking(null);
    }
  }

  async function endSession(id: string) {
    await api(`/api/admin/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => undefined);
    await client.invalidateQueries({ queryKey: ["admin", "sessions"] });
  }

  return (
    <div className="settings-stack">
      <section className="field-group" aria-label="Single sign-on">
        <h2 className="field-heading">Single sign-on</h2>
        <p>{sso.data?.enabled ? "Microsoft Entra sign-in is enabled." : "Microsoft Entra sign-in is not configured."}</p>
        <p className="muted field-hint">Sign-in providers are configured with environment variables on the NVR host. Their secrets cannot be viewed or changed here.</p>
      </section>

      <SignInSecurity />

      <CameraAccess />

      <section className="field-group" aria-label="Active sessions">
        <h2 className="field-heading">Active sessions</h2>
        {sessions.data?.length ? (
          <table className="inventory-table">
            <thead><tr><th scope="col">User</th><th scope="col">Role</th><th scope="col">Signed in</th><th scope="col">Expires</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {sessions.data.map(session => (
                <tr key={session.session_id}>
                  <td>{session.username}</td><td>{session.role}</td><td>{formatRelative(session.created_at)}</td><td>{formatRelative(session.expires_at)}</td>
                  <td><button type="button" className="button" data-variant="ghost" onClick={() => void endSession(session.session_id)}>End session</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">{sessions.isPending ? "Loading…" : "No active sessions."}</p>}
      </section>

      <section className="field-group" aria-label="API tokens">
        <h2 className="field-heading">API tokens</h2>
        <p className="muted field-hint">Tokens let other systems use the API. Each one acts for the administrator who created it, limited to its scopes. The secret is shown once.</p>
        <form className="tab-form" aria-label="New API token" onSubmit={event => void create(event)}>
          <label className="field"><span>Name</span><input value={name} maxLength={80} onChange={event => setName(event.target.value)} /></label>
          <fieldset className="plain">
            <legend className="field-heading">Scopes</legend>
            {(tokens.data?.scopes ?? ["read", "media", "export", "metrics", "admin"]).map(scope => (
              <label key={scope} className="check">
                <input type="checkbox" checked={scopes.includes(scope)} onChange={event => setScopes(event.target.checked ? [...scopes, scope] : scopes.filter(item => item !== scope))} />
                {scope} <span className="muted">· {SCOPE_HINT[scope] ?? ""}</span>
              </label>
            ))}
          </fieldset>
          <label className="field"><span>Expires (optional)</span><input type="datetime-local" value={expires} onChange={event => setExpires(event.target.value)} /></label>
          {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={!name.trim() || !scopes.length}>Create token</button></div>
        </form>
        {tokens.data?.tokens.length ? (
          <table className="inventory-table" aria-label="Existing API tokens">
            <thead><tr><th scope="col">Name</th><th scope="col">Scopes</th><th scope="col">State</th><th scope="col">Last used</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {tokens.data.tokens.map(token => (
                <tr key={token.id} data-state={token.state}>
                  <td>{token.name}</td>
                  <td>{token.scopes.join(", ")}</td>
                  <td>{token.state}{token.expires_at && token.state === "active" ? ` · expires ${formatRelative(token.expires_at)}` : ""}</td>
                  <td>{token.last_used_at ? formatRelative(token.last_used_at) : "Never"}</td>
                  <td>{token.state === "active" ? <button type="button" className="button" data-variant="ghost" onClick={() => setRevoking(token)}>Revoke</button> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">{tokens.isPending ? "Loading…" : "No API tokens."}</p>}
      </section>
      <SecretDialog secret={secret} title="API token created" notice="Copy this token now. It is shown once and cannot be retrieved later." onClose={() => setSecret(null)} />
      <Modal open={revoking !== null} onOpenChange={open => !open && setRevoking(null)} title={`Revoke ${revoking?.name ?? "token"}?`}
             description="Anything using this token stops working immediately."
             footer={<><button type="button" className="button" onClick={() => setRevoking(null)}>Cancel</button><button type="button" className="button" data-variant="danger" onClick={() => revoking && void revoke(revoking)}>Revoke token</button></>}>
        <p className="muted">This cannot be undone; create a new token if it is needed again.</p>
      </Modal>
    </div>
  );
}
