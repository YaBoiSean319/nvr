import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface Policy { scope: "default" | "group" | "camera"; id: string | null; name: string; retention_days: number; purpose: string; legal_basis: string; needs_purpose: boolean }
interface Settings {
  require_purpose: boolean; default_purpose: string; default_legal_basis: string; organisation: string; contact_name: string; contact_email: string; contact_phone: string; policies: Policy[];
}
interface Deletion { camera_id: string; camera_name: string; day: string; segments: number; bytes: number; retention_days: number; purpose: string }

const KEY = ["admin", "privacy-notices"] as const;
const DETAILS = ["organisation", "contact_name", "contact_email", "contact_phone", "default_purpose", "default_legal_basis"] as const;
const DETAIL_LABEL: Record<(typeof DETAILS)[number], string> = {
  organisation: "Organisation", contact_name: "Contact name", contact_email: "Contact email", contact_phone: "Contact phone",
  default_purpose: "Purpose of the site default policy", default_legal_basis: "Legal basis of the site default policy",
};

const megabytes = (bytes: number) => `${(bytes / 1_000_000).toFixed(1)} MB`;

function PolicyRow({ policy, onSaved, onError }: { policy: Policy; onSaved: () => void; onError: (text: string) => void }) {
  const [purpose, setPurpose] = useState(policy.purpose);
  const [basis, setBasis] = useState(policy.legal_basis);
  const save = useMutation({
    mutationFn: () => policy.scope === "default"
      ? api("/api/privacy/settings", { method: "PUT", body: { default_purpose: purpose, default_legal_basis: basis } })
      : api(`/api/${policy.scope === "group" ? "camera-groups" : "cameras"}/${policy.id}/retention`, { method: "PUT", body: { retention_days: policy.retention_days, purpose, legal_basis: basis } }),
    onSuccess: () => { onError(""); onSaved(); },
    onError: problem => onError(failureText(problem, "The policy could not be saved.")),
  });
  return (
    <li>
      <span className="row-select">{policy.scope === "default" ? policy.name : `${policy.scope === "group" ? "Group" : "Camera"} ${policy.name}`}</span>
      <span className="muted">{policy.scope === "default" ? "30 days unless overridden" : `${policy.retention_days} days`}{policy.needs_purpose ? " · needs a purpose" : ""}</span>
      <span className="field-actions">
        <label className="field"><span>Purpose for {policy.name}</span><input value={purpose} maxLength={300} onChange={event => setPurpose(event.target.value)} /></label>
        <label className="field"><span>Legal basis for {policy.name}</span><input value={basis} maxLength={300} onChange={event => setBasis(event.target.value)} /></label>
        <button type="button" className="button" disabled={save.isPending} onClick={() => save.mutate()}>Save {policy.name}</button>
      </span>
    </li>
  );
}

/** Why footage is kept, what retention deleted, and the camera notice and data-protection summary generated from the configuration. */
export function PrivacySettings() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<Settings>("/api/privacy/settings", { signal }) });
  const deletions = useQuery({ queryKey: ["admin", "retention-deletions"], queryFn: ({ signal }) => api<{ deletions: Deletion[] }>("/api/retention-deletions?limit=200", { signal }).then(result => result.deletions) });
  const [edits, setEdits] = useState<Partial<Settings>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const value = settings.data ? { ...settings.data, ...edits } : undefined;
  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const save = useMutation({
    mutationFn: () => api<Settings>("/api/privacy/settings", { method: "PUT", body: Object.fromEntries(Object.entries(edits).filter(([key]) => key !== "policies")) }),
    onSuccess: async () => { setError(null); setMessage("Saved."); setEdits({}); await refresh(); },
    onError: problem => setError(failureText(problem, "The settings could not be saved.")),
  });
  if (!value) return <p className="muted">Loading…</p>;
  const missing = value.policies.filter(policy => policy.needs_purpose);

  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Purpose and contact">
        <h2 className="field-heading">Purpose and contact</h2>
        <p className="muted field-hint">Record why each retention policy keeps footage, and who people can ask about it. The camera notice and the data-protection summary are built from these and from the cameras' settings.</p>
        <label className="check"><input type="checkbox" checked={value.require_purpose} onChange={event => setEdits({ ...edits, require_purpose: event.target.checked })} /> Require a purpose for every retention policy</label>
        <div className="field-row">
          {DETAILS.map(key => (
            <label key={key} className="field"><span>{DETAIL_LABEL[key]}</span><input value={value[key]} maxLength={300} onChange={event => setEdits({ ...edits, [key]: event.target.value })} /></label>
          ))}
        </div>
        <div className="field-actions"><button type="button" className="button" data-variant="primary" disabled={save.isPending || Object.keys(edits).length === 0} onClick={() => save.mutate()}>Save privacy settings</button></div>
      </section>

      <section className="field-group settings-block" aria-label="Retention policies and purposes">
        <h2 className="field-heading">Retention policies and purposes</h2>
        {missing.length ? <p className="notice" data-tone="warn" role="status">{missing.length} {missing.length === 1 ? "policy needs" : "policies need"} a purpose before it can be changed: {missing.map(policy => policy.name).join(", ")}.</p> : null}
        <ul className="status-list" aria-label="Policies">
          {value.policies.map(policy => <PolicyRow key={`${policy.scope}-${policy.id}-${policy.purpose}-${policy.legal_basis}`} policy={policy} onSaved={() => { setMessage("Saved."); void refresh(); }} onError={setError} />)}
        </ul>
      </section>

      <section className="field-group settings-block" aria-label="Notices">
        <h2 className="field-heading">Camera notice and data-protection summary</h2>
        <p className="muted field-hint">Generated from the current settings each time you open them; cameras are named, never their addresses or logins. Print them or save them as PDF from the page.</p>
        <div className="field-actions">
          <a className="button" href="/api/privacy/notice?format=html" target="_blank" rel="noreferrer">Open the camera notice</a>
          <a className="button" href="/api/privacy/summary?format=html" target="_blank" rel="noreferrer">Open the data-protection summary</a>
        </div>
      </section>

      <section className="field-group settings-block" aria-label="Automatic deletions">
        <h2 className="field-heading">Automatic deletions</h2>
        <p className="muted field-hint">What retention removed for each camera each day, with the policy and purpose in force at the time.</p>
        <table className="data-table" aria-label="Deletion record">
          <thead><tr><th>Day</th><th>Camera</th><th>Recordings</th><th>Size</th><th>Kept for</th><th>Purpose</th></tr></thead>
          <tbody>
            {(deletions.data ?? []).map(item => (
              <tr key={`${item.camera_id}-${item.day}`}><td>{item.day}</td><td>{item.camera_name}</td><td>{item.segments}</td><td>{megabytes(item.bytes)}</td><td>{item.retention_days} days</td><td>{item.purpose || "Not recorded"}</td></tr>
            ))}
            {deletions.data?.length === 0 ? <tr><td colSpan={6} className="muted">Nothing has been deleted automatically yet.</td></tr> : null}
          </tbody>
        </table>
      </section>
    </>
  );
}
