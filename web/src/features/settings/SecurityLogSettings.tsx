import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { formatSiteDateTime } from "@/lib/zonedTime";

interface Entry { id: number; at: string; actor_id: number | null; actor: string | null; action: string; resource_type: string; resource_id: string; details: Record<string, unknown> }
interface Log { total: number; entries: Entry[]; actions: string[] }
interface Person { id: number; username: string }
interface Retention { retention_days: number; minimum: number; maximum: number }

const DAY = 86_400_000;
const PAGE = 100;
const dateInput = (moment: number) => { const date = new Date(moment); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`; };
/** "mfa_reset" reads as "Mfa reset": the stored name, made legible, so it still matches the CSV and the docs. */
const label = (action: string) => { const words = action.replace(/_/g, " "); return words.charAt(0).toUpperCase() + words.slice(1); };
const detailText = (details: Record<string, unknown>) => Object.entries(details).map(([key, value]) => `${key.replace(/_/g, " ")}: ${typeof value === "object" && value !== null ? JSON.stringify(value) : String(value)}`).join(" · ");

/** Every recorded change to accounts, sign-in and settings, for a date range, by person and by action. */
export function SecurityLogSettings() {
  const display = useDisplaySettings();
  const [from, setFrom] = useState(() => dateInput(Date.now() - 6 * DAY));
  const [to, setTo] = useState(() => dateInput(Date.now()));
  const [person, setPerson] = useState("");
  const [action, setAction] = useState("");
  const [page, setPage] = useState(0);

  const start = new Date(`${from}T00:00:00`);
  const end = new Date(`${to}T00:00:00`);
  end.setDate(end.getDate() + 1);
  const valid = !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) && start < end;
  const filters = valid ? { from: start.toISOString(), to: end.toISOString(), ...(person ? { user_id: person } : {}), ...(action ? { action } : {}) } : null;
  const query = (extra: Record<string, string> = {}) => new URLSearchParams({ ...filters, ...extra });

  const people = useQuery({ queryKey: ["admin", "users"], queryFn: ({ signal }) => api<{ users: Person[] }>("/api/admin/users", { signal }).then(result => result.users) });
  const log = useQuery({
    queryKey: ["security-log", "entries", filters, page],
    enabled: filters !== null,
    queryFn: ({ signal }) => api<Log>(`/api/admin/security-log?${query({ limit: String(PAGE), offset: String(page * PAGE) })}`, { signal }),
  });
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
  const change = (set: (value: string) => void) => (value: string) => { set(value); setPage(0); };
  const pages = log.data ? Math.max(1, Math.ceil(log.data.total / PAGE)) : 1;

  return (
    <>
      <section className="field-group settings-block" aria-label="Security log">
        <h2 className="field-heading">Security log</h2>
        <p className="muted field-hint">
          Changes to accounts, sign-in, API tokens, camera access and settings, and sign-ins that were slowed or refused. Who watched which camera is in the viewing audit.
          A row with no person was done by the NVR itself, from the NVR host, or by someone who was not signed in.
        </p>
        <div className="field-actions">
          <label className="field"><span>From</span><input type="date" value={from} max={to} onChange={event => change(setFrom)(event.target.value)} /></label>
          <label className="field"><span>To</span><input type="date" value={to} min={from} onChange={event => change(setTo)(event.target.value)} /></label>
          <label className="field"><span>Person</span>
            <select value={person} onChange={event => change(setPerson)(event.target.value)}>
              <option value="">Everyone</option>
              {(people.data ?? []).map(user => <option key={user.id} value={String(user.id)}>{user.username}</option>)}
            </select>
          </label>
          <label className="field"><span>Action</span>
            <select value={action} onChange={event => change(setAction)(event.target.value)}>
              <option value="">Every action</option>
              {(log.data?.actions ?? (action ? [action] : [])).map(name => <option key={name} value={name}>{label(name)}</option>)}
            </select>
          </label>
        </div>
        {!valid ? <p className="notice" data-tone="warn" role="note">Choose a range that ends on or after the day it starts.</p> : null}
        {log.isPending && valid ? <p className="muted">Loading…</p> : log.error ? <p className="notice" data-tone="bad" role="alert">{failureText(log.error)}</p> : log.data ? (
          log.data.entries.length ? (
            <>
              <div className="inventory-scroll">
                <table className="inventory-table" aria-label="Security log entries">
                  <thead><tr><th scope="col">When</th><th scope="col">Person</th><th scope="col">Action</th><th scope="col">On</th><th scope="col">Details</th></tr></thead>
                  <tbody>
                    {log.data.entries.map(entry => (
                      <tr key={entry.id}>
                        <td>{when(entry.at)}</td>
                        <td>{entry.actor ?? (entry.actor_id === null ? "—" : "Removed account")}</td>
                        <td>{label(entry.action)}</td>
                        <td>{entry.resource_type.replace(/_/g, " ")} {entry.resource_id}</td>
                        <td>{detailText(entry.details)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="field-actions">
                <span className="muted" role="status">{log.data.total} {log.data.total === 1 ? "entry" : "entries"}; page {page + 1} of {pages}.</span>
                <button type="button" className="button" data-variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Newer</button>
                <button type="button" className="button" data-variant="ghost" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Older</button>
              </p>
            </>
          ) : <p className="muted">Nothing was recorded in this range.</p>
        ) : null}
        {filters ? <p className="field-actions"><a className="button" href={`/api/admin/security-log/export.csv?${query()}`} download>Download as CSV</a></p> : null}
      </section>
      <RetentionSection />
    </>
  );
}

function RetentionSection() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["security-log", "settings"], queryFn: ({ signal }) => api<Retention>("/api/admin/security-log/settings", { signal }) });
  const [draft, setDraft] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const value = draft ?? String(settings.data?.retention_days ?? "");
  const save = useMutation({
    mutationFn: () => api<Retention & { removed: number }>("/api/admin/security-log/settings", { method: "PUT", body: { retention_days: Number(value) } }),
    onSuccess: result => {
      setDraft(null);
      setNotice(result.removed ? `Kept for ${result.retention_days} days; ${result.removed} older ${result.removed === 1 ? "entry was" : "entries were"} removed.` : `Kept for ${result.retention_days} days.`);
      client.setQueryData(["security-log", "settings"], result);
      void client.invalidateQueries({ queryKey: ["security-log", "entries"] });
    },
    onError: () => setNotice(null),
  });
  return (
    <section className="field-group settings-block" aria-label="Security log retention">
      <h2 className="field-heading">Security log retention</h2>
      <p className="muted field-hint">Entries older than this are removed, separately from the viewing audit and from how long footage is kept.</p>
      <form className="tab-form" onSubmit={event => { event.preventDefault(); save.mutate(); }} noValidate>
        <label className="field"><span>Keep security log entries for (days)</span>
          <input type="number" min={settings.data?.minimum ?? 30} max={settings.data?.maximum ?? 3650} value={value} onChange={event => { setDraft(event.target.value); setNotice(null); }} />
        </label>
        {settings.data ? <p className="muted">Between {settings.data.minimum} and {settings.data.maximum} days.</p> : null}
        {notice ? <p className="notice" role="status" data-tone="good">{notice}</p> : null}
        {save.error ? <p className="notice" role="alert" data-tone="bad">{failureText(save.error)}</p> : null}
        <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={save.isPending || !value}>Save retention</button></div>
      </form>
    </section>
  );
}
