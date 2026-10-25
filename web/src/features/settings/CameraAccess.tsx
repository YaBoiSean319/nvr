import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";

interface Grant {
  id: number; subject_type: "user" | "role"; subject: string; subject_name: string; scope_type: "all" | "group";
  group_id: string | null; group_name: string | null; expires_at: string | null; expired: boolean;
}
interface Listing {
  grants: Grant[];
  operators: { id: number; username: string; local: boolean }[];
  roles: string[];
  groups: { id: string; name: string }[];
}
interface Subject { type: "user" | "role"; key: string; label: string }
type Mode = "all" | "groups" | "none";

/** An ISO time as the value of a datetime-local input, in the browser's zone. */
const toLocalInput = (iso: string | null) => {
  if (!iso) return "";
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};
const toIso = (local: string) => (local ? new Date(local).toISOString() : null);
const when = (iso: string) => new Date(iso).toLocaleString();

function describe(grants: Grant[]): string {
  if (!grants.length) return "No cameras";
  return grants.map(grant => {
    const scope = grant.scope_type === "all" ? "All cameras" : grant.group_name ?? "A removed group";
    return grant.expires_at ? `${scope} ${grant.expired ? `(expired ${when(grant.expires_at)})` : `until ${when(grant.expires_at)}`}` : scope;
  }).join(", ");
}

/** Which cameras each operator (or Entra role) may see, and until when. Administrators always see every camera. */
export function CameraAccess() {
  const client = useQueryClient();
  const listing = useQuery({ queryKey: ["admin", "access-grants"], queryFn: ({ signal }) => api<Listing>("/api/admin/access-grants", { signal }) });
  const [editing, setEditing] = useState<Subject | null>(null);
  const [newRole, setNewRole] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const data = listing.data;
  const grantsOf = (subject: Subject) => (data?.grants ?? []).filter(grant => grant.subject_type === subject.type && grant.subject === subject.key);
  const roleSubjects = [...new Set([...(data?.roles ?? []), ...(data?.grants ?? []).filter(grant => grant.subject_type === "role").map(grant => grant.subject)])]
    .filter(role => (data?.grants ?? []).some(grant => grant.subject_type === "role" && grant.subject === role));

  return (
    <section className="field-group" aria-label="Camera access">
      <h2 className="field-heading">Camera access</h2>
      <p className="muted field-hint">
        An operator sees only the cameras their grants give them: every camera, or the cameras of chosen groups, optionally until an expiry (for a contractor or an investigator).
        Everything else, including its footage, alarms and exports, is hidden as if it did not exist. Operators start with every camera until you narrow them; administrators always see every camera.
        Grants given to a Microsoft Entra role apply to everyone holding it. Changes apply on the person's next request, and cut open live video within a second.
      </p>
      {notice ? <p className="notice" role="status" data-tone="good">{notice}</p> : null}
      {listing.error ? <p className="notice" role="alert" data-tone="bad">{failureText(listing.error)}</p> : null}
      {data ? (
        <>
          <table className="inventory-table" aria-label="Camera access by operator">
            <thead><tr><th scope="col">Operator</th><th scope="col">Cameras</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
            <tbody>
              {data.operators.length ? data.operators.map(operator => {
                const subject: Subject = { type: "user", key: String(operator.id), label: operator.username };
                return (
                  <tr key={operator.id}>
                    <td>{operator.username}</td>
                    <td>{describe(grantsOf(subject))}</td>
                    <td><button type="button" className="button" data-variant="ghost" aria-label={`Change camera access for ${operator.username}`} onClick={() => setEditing(subject)}>Change</button></td>
                  </tr>
                );
              }) : <tr><td colSpan={3} className="muted">No operators.</td></tr>}
            </tbody>
          </table>

          <h3 className="field-heading">Entra roles</h3>
          {roleSubjects.length ? (
            <table className="inventory-table" aria-label="Camera access by Entra role">
              <thead><tr><th scope="col">Role</th><th scope="col">Cameras</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
              <tbody>
                {roleSubjects.map(role => {
                  const subject: Subject = { type: "role", key: role, label: role };
                  return (
                    <tr key={role}>
                      <td>{role}</td><td>{describe(grantsOf(subject))}</td>
                      <td><button type="button" className="button" data-variant="ghost" aria-label={`Change camera access for role ${role}`} onClick={() => setEditing(subject)}>Change</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <p className="muted">No role has been given access. An Entra operator whose role has no grant here starts with every camera.</p>}
          <form className="field-actions" aria-label="Give an Entra role access" onSubmit={event => { event.preventDefault(); if (newRole.trim()) { setEditing({ type: "role", key: newRole.trim(), label: newRole.trim() }); setNewRole(""); } }}>
            <label className="field"><span>Entra role</span>
              <input value={newRole} list="entra-roles" maxLength={200} placeholder="NVR.Contractor" onChange={event => setNewRole(event.target.value)} />
              <datalist id="entra-roles">{data.roles.map(role => <option key={role} value={role} />)}</datalist>
            </label>
            <button type="submit" className="button" disabled={!newRole.trim()}>Choose cameras…</button>
          </form>
          <GrantDialog subject={editing} listing={data} grants={editing ? grantsOf(editing) : []} onClose={() => setEditing(null)}
                       onSaved={message => { setNotice(message); setEditing(null); void client.invalidateQueries({ queryKey: ["admin", "access-grants"] }); }} />
        </>
      ) : listing.isPending ? <p className="muted">Loading…</p> : null}
    </section>
  );
}

function GrantDialog({ subject, listing, grants, onClose, onSaved }: { subject: Subject | null; listing: Listing; grants: Grant[]; onClose: () => void; onSaved: (message: string) => void }) {
  return (
    <Modal open={subject !== null} onOpenChange={open => { if (!open) onClose(); }} title={`Camera access for ${subject?.label ?? ""}`}
           description={subject?.type === "role" ? "Everyone holding this Entra role gets this access." : "Choose which cameras this operator may see."} wide
           footer={<button type="button" className="button" onClick={onClose}>Cancel</button>}>
      {subject ? <GrantForm key={`${subject.type}:${subject.key}`} subject={subject} listing={listing} grants={grants} onSaved={onSaved} /> : null}
    </Modal>
  );
}

function GrantForm({ subject, listing, grants, onSaved }: { subject: Subject; listing: Listing; grants: Grant[]; onSaved: (message: string) => void }) {
  const initialMode: Mode = grants.some(grant => grant.scope_type === "all") ? "all" : grants.length ? "groups" : "none";
  const [mode, setMode] = useState<Mode>(initialMode);
  const [allExpiry, setAllExpiry] = useState(toLocalInput(grants.find(grant => grant.scope_type === "all")?.expires_at ?? null));
  const [groups, setGroups] = useState<Record<string, string | null>>(
    Object.fromEntries(grants.filter(grant => grant.scope_type === "group" && grant.group_id).map(grant => [grant.group_id as string, toLocalInput(grant.expires_at)])));
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => {
      const body = mode === "all" ? [{ scope_type: "all", expires_at: toIso(allExpiry) }]
        : mode === "groups" ? Object.entries(groups).map(([group_id, expiry]) => ({ scope_type: "group", group_id, expires_at: toIso(expiry ?? "") }))
          : [];
      return api("/api/admin/access-grants", { method: "PUT", body: { subject_type: subject.type, subject: subject.key, grants: body } });
    },
    onSuccess: () => onSaved(`Camera access updated for ${subject.label}.`),
    onError: failure => setError(failureText(failure)),
  });
  const none = mode === "groups" && Object.keys(groups).length === 0;

  return (
    <form className="tab-form" aria-label={`Camera access for ${subject.label}`} onSubmit={event => { event.preventDefault(); setError(null); save.mutate(); }} noValidate>
      <fieldset className="plain">
        <legend className="field-heading">Cameras</legend>
        <label className="check"><input type="radio" name="access-mode" checked={mode === "all"} onChange={() => setMode("all")} /> Every camera</label>
        <label className="check"><input type="radio" name="access-mode" checked={mode === "groups"} onChange={() => setMode("groups")} /> Only the cameras of chosen groups</label>
        <label className="check"><input type="radio" name="access-mode" checked={mode === "none"} onChange={() => setMode("none")} /> No cameras</label>
      </fieldset>
      {mode === "all" ? (
        <label className="field"><span>Until (optional)</span><input type="datetime-local" value={allExpiry} onChange={event => setAllExpiry(event.target.value)} /></label>
      ) : null}
      {mode === "groups" ? (
        listing.groups.length ? (
          <ul className="plain-list" aria-label="Camera groups">
            {listing.groups.map(group => (
              <li key={group.id}>
                <label className="check">
                  <input type="checkbox" checked={group.id in groups}
                         onChange={event => setGroups(current => { const next = { ...current }; if (event.target.checked) next[group.id] = ""; else delete next[group.id]; return next; })} />
                  {" "}{group.name}
                </label>
                {group.id in groups ? (
                  <label className="field"><span>{group.name} until (optional)</span>
                    <input type="datetime-local" value={groups[group.id] ?? ""} onChange={event => setGroups(current => ({ ...current, [group.id]: event.target.value }))} />
                  </label>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <p className="muted">There are no camera groups yet. Create groups under Cameras › Groups, then choose them here.</p>
      ) : null}
      {mode === "none" || none ? <p className="notice" role="note" data-tone="warn">With no cameras this person can sign in but sees nothing.</p> : null}
      {error ? <p className="notice" role="alert" data-tone="bad">{error}</p> : null}
      <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={save.isPending}>Save access</button></div>
    </form>
  );
}
