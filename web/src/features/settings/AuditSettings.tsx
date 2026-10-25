import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { formatSiteDateTime } from "@/lib/zonedTime";

type By = "person" | "camera";
interface Totals { live_sessions: number; live_seconds: number; playback_sessions: number; playback_seconds: number; exports: number; downloads: number; snapshots: number }
interface Row extends Totals { user_id?: number | null; username?: string; cameras?: number; camera_id?: string; camera_name?: string; people?: number }
interface Report { rows: Row[]; total: Totals & { entries: number } }
interface Entry { id: number; username: string; camera_name: string; kind: string; started_at: string; ended_at: string | null; open: boolean; seconds: number; quality: string | null }
interface Entries { total: number; entries: Entry[] }
interface Retention { retention_days: number; minimum: number; maximum: number }

const DAY = 86_400_000;
const KIND_LABEL: Record<string, string> = { live: "Live", playback: "Playback", export: "Export", download: "Download", snapshot: "Snapshot" };

/** A date input's value for a moment, in the browser's zone. */
const dateInput = (moment: number) => { const date = new Date(moment); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`; };

function duration(seconds: number): string {
  if (seconds < 1) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return minutes % 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes / 60} h`;
}

/** Who watched which camera, live or in playback, and every export, download and snapshot, for a date range. */
export function AuditSettings() {
  const display = useDisplaySettings();
  const [from, setFrom] = useState(() => dateInput(Date.now() - 6 * DAY));
  const [to, setTo] = useState(() => dateInput(Date.now()));
  const [by, setBy] = useState<By>("person");
  const [focus, setFocus] = useState<{ user_id?: number; camera_id?: string; label: string } | null>(null);

  // The range runs from the start of the first day to the end of the last, in the browser's zone.
  const start = new Date(`${from}T00:00:00`);
  const end = new Date(`${to}T00:00:00`);
  end.setDate(end.getDate() + 1);
  const valid = !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) && start < end;
  const range = valid ? { from: start.toISOString(), to: end.toISOString() } : null;
  const query = (extra: Record<string, string | undefined> = {}) => new URLSearchParams(Object.entries({ ...range, ...extra }).filter((entry): entry is [string, string] => entry[1] !== undefined));
  const filter = { user_id: focus?.user_id === undefined ? undefined : String(focus.user_id), camera_id: focus?.camera_id };

  const report = useQuery({
    queryKey: ["audit", "report", by, range],
    enabled: range !== null,
    queryFn: ({ signal }) => api<Report>(`/api/admin/viewing-audit/report?${query({ by })}`, { signal }),
  });
  const entries = useQuery({
    queryKey: ["audit", "entries", range, focus],
    enabled: range !== null,
    queryFn: ({ signal }) => api<Entries>(`/api/admin/viewing-audit/entries?${query({ ...filter, limit: "100" })}`, { signal }),
  });
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);

  return (
    <>
      <section className="field-group settings-block" aria-label="Viewing report">
        <h2 className="field-heading">Viewing report</h2>
        <p className="muted field-hint">
          Who watched which camera, live or in playback, from when to when, and every export, download and snapshot. A live tile is one session from opening to closing;
          playback is one session until two minutes pass without a recording being requested. The report by person and by camera add up to the same totals.
        </p>
        <div className="field-actions">
          <label className="field"><span>From</span><input type="date" value={from} max={to} onChange={event => setFrom(event.target.value)} /></label>
          <label className="field"><span>To</span><input type="date" value={to} min={from} onChange={event => setTo(event.target.value)} /></label>
        </div>
        <fieldset className="plain">
          <legend className="field-heading">Group by</legend>
          <label className="check"><input type="radio" name="audit-by" checked={by === "person"} onChange={() => { setBy("person"); setFocus(null); }} /> Person</label>
          <label className="check"><input type="radio" name="audit-by" checked={by === "camera"} onChange={() => { setBy("camera"); setFocus(null); }} /> Camera</label>
        </fieldset>
        {!valid ? <p className="notice" data-tone="warn" role="note">Choose a range that ends on or after the day it starts.</p> : null}
        {report.isPending && valid ? <p className="muted">Loading…</p> : report.error ? <p className="notice" data-tone="bad" role="alert">{failureText(report.error)}</p> : report.data ? (
          report.data.rows.length ? (
            <div className="inventory-scroll">
              <table className="inventory-table" aria-label={`Viewing by ${by}`}>
                <thead>
                  <tr>
                    <th scope="col">{by === "person" ? "Person" : "Camera"}</th><th scope="col">{by === "person" ? "Cameras" : "People"}</th>
                    <th scope="col">Live</th><th scope="col">Playback</th><th scope="col">Exports</th><th scope="col">Downloads</th><th scope="col">Snapshots</th><th scope="col"><span className="visually-hidden">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {report.data.rows.map(row => {
                    const name = (by === "person" ? row.username : row.camera_name) ?? "";
                    return (
                      <tr key={by === "person" ? `${row.user_id}:${row.username}` : row.camera_id}>
                        <th scope="row">{name}</th>
                        <td>{by === "person" ? row.cameras : row.people}</td>
                        <td>{row.live_sessions} · {duration(row.live_seconds)}</td>
                        <td>{row.playback_sessions} · {duration(row.playback_seconds)}</td>
                        <td>{row.exports}</td><td>{row.downloads}</td><td>{row.snapshots}</td>
                        <td>
                          <button type="button" className="button" data-variant="ghost" aria-label={`Show entries for ${name}`}
                                  onClick={() => setFocus(by === "person" ? { user_id: row.user_id ?? undefined, label: name } : { camera_id: row.camera_id, label: name })}>Entries</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th><td />
                    <td>{report.data.total.live_sessions} · {duration(report.data.total.live_seconds)}</td>
                    <td>{report.data.total.playback_sessions} · {duration(report.data.total.playback_seconds)}</td>
                    <td>{report.data.total.exports}</td><td>{report.data.total.downloads}</td><td>{report.data.total.snapshots}</td><td />
                  </tr>
                </tfoot>
              </table>
            </div>
          ) : valid ? <p className="muted">Nothing was viewed in this range.</p> : null
        ) : null}
        {range ? (
          <p className="field-actions">
            <a className="button" href={`/api/admin/viewing-audit/export.csv?${query({ view: by })}`} download>Download CSV by {by}</a>
            <a className="button" data-variant="ghost" href={`/api/admin/viewing-audit/export.csv?${query({ view: "entries" })}`} download>Download every entry as CSV</a>
          </p>
        ) : null}
      </section>

      <section className="field-group settings-block" aria-label="Viewing entries">
        <h2 className="field-heading">Entries{focus ? ` for ${focus.label}` : ""}</h2>
        {focus ? <div className="field-actions"><button type="button" className="button" data-variant="ghost" onClick={() => setFocus(null)}>Show everyone and every camera</button></div> : null}
        {entries.isPending && valid ? <p className="muted">Loading…</p> : entries.error ? <p className="notice" data-tone="bad" role="alert">{failureText(entries.error)}</p> : entries.data ? (
          entries.data.entries.length ? (
            <>
              <div className="inventory-scroll">
                <table className="inventory-table" aria-label="Viewing entries">
                  <thead><tr><th scope="col">When</th><th scope="col">Person</th><th scope="col">Camera</th><th scope="col">What</th><th scope="col">Length</th></tr></thead>
                  <tbody>
                    {entries.data.entries.map(entry => (
                      <tr key={entry.id}>
                        <td>{when(entry.started_at)}</td><td>{entry.username}</td><td>{entry.camera_name}</td>
                        <td>{KIND_LABEL[entry.kind] ?? entry.kind}{entry.quality ? ` (${entry.quality})` : ""}{entry.open ? " · watching now" : ""}</td>
                        <td>{entry.kind === "live" || entry.kind === "playback" ? duration(entry.seconds) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {entries.data.total > entries.data.entries.length ? <p className="muted">Showing the latest {entries.data.entries.length} of {entries.data.total}; download the CSV for all of them.</p> : null}
            </>
          ) : valid ? <p className="muted">No entries.</p> : null
        ) : null}
      </section>

      <RetentionSection />
    </>
  );
}

function RetentionSection() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["audit", "settings"], queryFn: ({ signal }) => api<Retention>("/api/admin/viewing-audit/settings", { signal }) });
  const [draft, setDraft] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const value = draft ?? String(settings.data?.retention_days ?? "");
  const save = useMutation({
    mutationFn: () => api<Retention & { removed: number }>("/api/admin/viewing-audit/settings", { method: "PUT", body: { retention_days: Number(value) } }),
    onSuccess: result => {
      setDraft(null);
      setNotice(result.removed ? `Kept for ${result.retention_days} days; ${result.removed} older ${result.removed === 1 ? "entry was" : "entries were"} removed.` : `Kept for ${result.retention_days} days.`);
      client.setQueryData(["audit", "settings"], result);
      void client.invalidateQueries({ queryKey: ["audit"] });
    },
    onError: () => setNotice(null),
  });
  return (
    <section className="field-group settings-block" aria-label="Audit retention">
      <h2 className="field-heading">Audit retention</h2>
      <p className="muted field-hint">
        Viewing entries older than this are removed, separately from how long footage is kept. The security log of changes to settings and accounts is not affected.
      </p>
      <form className="tab-form" onSubmit={event => { event.preventDefault(); save.mutate(); }} noValidate>
        <label className="field"><span>Keep viewing entries for (days)</span>
          <input type="number" min={settings.data?.minimum ?? 7} max={settings.data?.maximum ?? 3650} value={value} onChange={event => { setDraft(event.target.value); setNotice(null); }} />
        </label>
        {settings.data ? <p className="muted">Between {settings.data.minimum} and {settings.data.maximum} days.</p> : null}
        {notice ? <p className="notice" role="status" data-tone="good">{notice}</p> : null}
        {save.error ? <p className="notice" role="alert" data-tone="bad">{failureText(save.error)}</p> : null}
        <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={save.isPending || !value}>Save retention</button></div>
      </form>
    </section>
  );
}
