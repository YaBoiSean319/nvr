import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { FailoverStatus } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";

interface Settings { role: "off" | "primary" | "standby"; peer_url: string | null; has_secret: boolean; allow_private: boolean; silence_seconds: number; lease_seconds: number; sync_seconds: number }
interface Episode { id: string; epoch: number; role: "primary" | "standby"; silent_since: string | null; started_at: string; ended_at: string | null; state: string; segments_imported: number }
interface Response { settings: Settings; status: FailoverStatus; episodes: Episode[] }

const KEY = ["admin", "failover"] as const;
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
const ROLE_TEXT = { off: "Off", primary: "Primary: the NVR that normally records", standby: "Standby: records only while the primary is down" } as const;
const STATE_TEXT: Record<string, string> = { active: "Standby recording", handback: "Handing back", done: "Handed back" };

/** A standby NVR that takes over recording when the primary stops, copies the primary's configuration, and hands its footage back. */
export function FailoverSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<Response>("/api/admin/failover", { signal }), refetchInterval: 5_000 });
  const [form, setForm] = useState({ role: "off" as Settings["role"], peer_url: "", secret: "", allow_private: false, silence_seconds: 120, lease_seconds: 60, sync_seconds: 900 });
  const [loaded, setLoaded] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const saved = state.data?.settings;
    if (saved && !loaded) {
      setForm({ role: saved.role, peer_url: saved.peer_url ?? "", secret: "", allow_private: saved.allow_private, silence_seconds: saved.silence_seconds, lease_seconds: saved.lease_seconds, sync_seconds: saved.sync_seconds });
      setLoaded(true);
    }
  }, [state.data, loaded]);

  const save = useMutation({
    mutationFn: () => api<Response>("/api/admin/failover", {
      method: "PUT", body: { role: form.role, peer_url: form.role === "off" ? null : form.peer_url, secret: form.secret || undefined, allow_private: form.allow_private,
                             silence_seconds: form.silence_seconds, lease_seconds: form.lease_seconds, sync_seconds: form.sync_seconds },
    }),
    onSuccess: async () => { setForm(current => ({ ...current, secret: "" })); setError(null); setMessage("Saved."); await client.invalidateQueries({ queryKey: KEY }); await client.invalidateQueries({ queryKey: ["failover"] }); },
    onError: problem => { setMessage(null); setError(failureText(problem, "Failover could not be saved.")); },
  });
  const sync = useMutation({
    mutationFn: () => api("/api/admin/failover/sync", { method: "POST" }),
    onSuccess: async () => { setError(null); setMessage("The configuration will be copied from the primary in a moment."); await client.invalidateQueries({ queryKey: KEY }); },
    onError: problem => { setMessage(null); setError(failureText(problem, "The configuration copy could not be requested.")); },
  });

  const data = state.data;
  const standby = data?.settings.role === "standby";
  const number = (name: "silence_seconds" | "lease_seconds" | "sync_seconds") => (event: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [name]: event.target.value === "" ? Number.NaN : Number(event.target.value) });
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Failover state">
        <h2 className="field-heading">State</h2>
        <p role="status">{data?.status.headline ?? "Loading…"}</p>
        {data && data.status.role !== "off" ? (
          <p className="muted">
            {data.status.role === "standby" ? "Last heard from the primary" : "Last answer from the standby"} {when(data.status.last_contact_at)}
            {data.status.role === "standby" ? ` · configuration copied ${when(data.status.last_sync_at)}${data.status.config_created_at ? ` (the primary's backup of ${when(data.status.config_created_at)})` : ""}` : ""}
            {data.status.last_error ? ` · ${data.status.last_error}` : ""}{data.status.last_sync_error ? ` · ${data.status.last_sync_error}` : ""}
          </p>
        ) : null}
        {standby ? <div className="field-actions"><button type="button" className="button" onClick={() => sync.mutate()} disabled={sync.isPending || data?.status.takeover_active}>Copy the primary's configuration now</button></div> : null}
      </section>

      <section className="field-group settings-block" aria-label="Failover settings">
        <h2 className="field-heading">Settings</h2>
        <p className="muted field-hint">
          A standby NVR records in the primary's place while the primary is down, then hands its footage back. The primary renews a lease with the standby every few seconds; when the standby has not heard it for the silence below it takes over and records every camera.
          The standby copies the primary's cameras, groups, schedules, retention and masks from the primary's encrypted backup (not footage, users or other settings). Set the standby up once by restoring a backup of the primary on it with
          <code> python -m app.backup restore --standby</code>, give both NVRs the same backup passphrase and the same failover secret, and enter each one's peer address here.
        </p>
        <form className="tab-form" aria-label="Failover" noValidate onSubmit={event => { event.preventDefault(); save.mutate(); }}>
          <div className="field-row">
            <label className="field"><span>This NVR is</span>
              <select value={form.role} onChange={event => setForm({ ...form, role: event.target.value as Settings["role"] })}>
                {(Object.keys(ROLE_TEXT) as Settings["role"][]).map(role => <option key={role} value={role}>{ROLE_TEXT[role]}</option>)}
              </select>
            </label>
            {form.role !== "off" ? (
              <>
                <label className="field"><span>{form.role === "primary" ? "Address of the standby" : "Address of the primary"}</span>
                  <input value={form.peer_url} placeholder="https://nvr-standby.example.com" onChange={event => setForm({ ...form, peer_url: event.target.value })} />
                </label>
                <label className="field"><span>Failover secret{data?.settings.has_secret ? " (one is saved)" : ""}</span>
                  <input type="password" autoComplete="off" value={form.secret} placeholder={data?.settings.has_secret ? "Leave empty to keep it" : "At least 16 characters, the same on both"} onChange={event => setForm({ ...form, secret: event.target.value })} />
                </label>
              </>
            ) : null}
          </div>
          {form.role !== "off" ? (
            <>
              <label className="check"><input type="checkbox" checked={form.allow_private} onChange={event => setForm({ ...form, allow_private: event.target.checked })} /> The other NVR is on a private network address</label>
              <div className="field-row">
                <label className="field"><span>Lease (seconds)</span><input type="number" min={5} max={1800} value={Number.isNaN(form.lease_seconds) ? "" : form.lease_seconds} onChange={number("lease_seconds")} /></label>
                <label className="field"><span>Silence before the standby takes over (seconds)</span><input type="number" min={10} max={3600} value={Number.isNaN(form.silence_seconds) ? "" : form.silence_seconds} onChange={number("silence_seconds")} /></label>
                <label className="field"><span>Copy the configuration every (seconds)</span><input type="number" min={60} max={86400} value={Number.isNaN(form.sync_seconds) ? "" : form.sync_seconds} onChange={number("sync_seconds")} /></label>
              </div>
              <p className="muted field-hint">The silence must be at least twice the lease. A shorter silence means a faster takeover and a greater chance of a takeover for a brief network fault.</p>
            </>
          ) : null}
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={save.isPending || (form.role !== "off" && !form.peer_url.trim())}>Save failover settings</button></div>
        </form>
      </section>

      <section className="field-group settings-block" aria-label="Takeovers">
        <h2 className="field-heading">Takeovers</h2>
        <ul className="status-list" aria-label="Takeover history">
          {(data?.episodes ?? []).map(item => (
            <li key={item.id}>
              <strong>{STATE_TEXT[item.state] ?? item.state}</strong>
              <span className="muted">
                {item.role === "standby" ? "This standby" : "The standby"} recorded from {when(item.started_at)}{item.ended_at ? ` to ${when(item.ended_at)}` : ""}
                {item.state === "done" ? ` · ${item.segments_imported} segments ${item.role === "standby" ? "handed back" : "brought back"}` : ""}
              </span>
            </li>
          ))}
          {data?.episodes.length === 0 ? <li className="muted">No takeovers yet.</li> : null}
        </ul>
      </section>
    </>
  );
}
