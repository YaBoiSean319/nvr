import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface Site {
  id: string; name: string; url: string; allow_private: boolean; enabled: boolean; state: "ok" | "unreachable" | "refused" | "disabled";
  last_contact_at: string | null; last_checked_at: string | null; last_error: string | null; camera_count: number | null; open_alarms: number | null;
}
interface Console { enabled: boolean; sites: Site[]; needed_scopes: string[] }

const KEY = ["admin", "console"] as const;
const STATE_TEXT: Record<Site["state"], string> = { ok: "Reachable", unreachable: "Unreachable", refused: "Refused the token", disabled: "Off" };
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

/** Other NVRs (sites) federated into this one: their cameras, live video, playback, alarms and search appear here, and each site keeps its own recording and settings. */
export function SitesSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<Console>("/api/console", { signal }), refetchInterval: 15_000 });
  const [form, setForm] = useState({ name: "", url: "", token: "", allow_private: false });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [replacing, setReplacing] = useState<{ id: string; token: string } | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: KEY }).then(() => client.invalidateQueries({ queryKey: ["cameras"] }));
  const fail = (problem: unknown, fallback: string) => { setMessage(null); setError(failureText(problem, fallback)); };

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api("/api/console", { method: "PUT", body: { enabled } }),
    onSuccess: async () => { setError(null); await refresh(); },
    onError: problem => fail(problem, "Console mode could not be changed."),
  });
  const add = useMutation({
    mutationFn: () => api("/api/console/sites", { method: "POST", body: { name: form.name, url: form.url, token: form.token, allow_private: form.allow_private } }),
    onSuccess: async () => { setForm({ name: "", url: "", token: "", allow_private: false }); setError(null); setMessage("Site added."); await refresh(); },
    onError: problem => fail(problem, "The site could not be added."),
  });
  const change = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) => api(`/api/console/sites/${id}`, { method: "PUT", body }),
    onSuccess: async () => { setReplacing(null); setError(null); setMessage("Saved."); await refresh(); },
    onError: problem => fail(problem, "The site could not be saved."),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/console/sites/${id}`, { method: "DELETE" }),
    onSuccess: async () => { setError(null); setMessage("Site removed."); await refresh(); },
    onError: problem => fail(problem, "The site could not be removed."),
  });
  const check = useMutation({
    mutationFn: (id: string) => api(`/api/console/sites/${id}/check`, { method: "POST" }),
    onSuccess: refresh,
    onError: problem => fail(problem, "The site could not be checked."),
  });

  const data = state.data;
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Console mode">
        <h2 className="field-heading">Console mode</h2>
        <p className="muted field-hint">
          A console shows the cameras of several NVRs (sites) in one place: live video and playback are relayed through each site, and alarms and search ask every site. Each site keeps its own recording, storage and
          settings, and no footage is copied here. Alarms are handled at the site that raised them.
        </p>
        <label className="check"><input type="checkbox" checked={data?.enabled ?? false} disabled={!data || toggle.isPending} onChange={event => toggle.mutate(event.target.checked)} /> This NVR is a console</label>
      </section>

      <section className="field-group settings-block" aria-label="Sites">
        <h2 className="field-heading">Sites</h2>
        <ul className="status-list" aria-label="Site list">
          {(data?.sites ?? []).map(site => (
            <li key={site.id}>
              <span className="row-select">{site.name}</span>
              <span className="muted" data-state={site.state}>
                {STATE_TEXT[site.state]} · {site.url} · last contact {when(site.last_contact_at)}
                {site.camera_count !== null ? ` · ${site.camera_count} cameras` : ""}{site.open_alarms !== null ? ` · ${site.open_alarms} open alarms` : ""}
                {site.last_error ? ` · ${site.last_error}` : ""}
              </span>
              <span className="field-actions">
                <button type="button" className="button" onClick={() => check.mutate(site.id)}>Check {site.name} now</button>
                <button type="button" className="button" onClick={() => change.mutate({ id: site.id, body: { enabled: !site.enabled } })}>{site.enabled ? `Turn ${site.name} off` : `Turn ${site.name} on`}</button>
                <button type="button" className="button" data-variant="ghost" onClick={() => setReplacing({ id: site.id, token: "" })}>Replace token of {site.name}</button>
                <button type="button" className="button" data-variant="ghost" onClick={() => remove.mutate(site.id)}>Remove {site.name}</button>
              </span>
              {replacing?.id === site.id ? (
                <form className="field-actions" aria-label={`Replace the token of ${site.name}`} onSubmit={event => { event.preventDefault(); change.mutate({ id: site.id, body: { token: replacing.token } }); }}>
                  <label className="field"><span>New token for {site.name}</span>
                    <input type="password" autoComplete="off" value={replacing.token} onChange={event => setReplacing({ id: site.id, token: event.target.value })} />
                  </label>
                  <button type="submit" className="button" data-variant="primary" disabled={replacing.token.length < 20 || change.isPending}>Save token</button>
                </form>
              ) : null}
            </li>
          ))}
          {data?.sites.length === 0 ? <li className="muted">No sites yet.</li> : null}
        </ul>

        <form className="tab-form" aria-label="Add a site" onSubmit={event => { event.preventDefault(); add.mutate(); }}>
          <h3 className="field-heading">Add a site</h3>
          <p className="muted field-hint">
            At the site, open Settings, Users &amp; access, and create an API token with only the <strong>read</strong> and <strong>media</strong> scopes. The console can then look and watch, but change nothing there.
            The token is stored encrypted and is never shown again.
          </p>
          <div className="field-row">
            <label className="field"><span>Site name</span><input value={form.name} maxLength={80} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
            <label className="field"><span>Site address</span><input value={form.url} placeholder="https://nvr.example.com" onChange={event => setForm({ ...form, url: event.target.value })} /></label>
            <label className="field"><span>Site token</span><input type="password" autoComplete="off" value={form.token} onChange={event => setForm({ ...form, token: event.target.value })} /></label>
          </div>
          <label className="check"><input type="checkbox" checked={form.allow_private} onChange={event => setForm({ ...form, allow_private: event.target.checked })} /> The site is on a private network address</label>
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={add.isPending || !form.name.trim() || !form.url.trim() || form.token.length < 20}>Add site</button></div>
        </form>
      </section>
    </>
  );
}
