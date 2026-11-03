import { useState, type FormEvent } from "react";
import { useCameraGroups, useCameras, useMe } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { useEndWindow, useMaintenance, useOpenWindow, type MaintenanceWindow } from "./api";

const DURATIONS: [number, string][] = [[30, "30 minutes"], [60, "1 hour"], [120, "2 hours"], [240, "4 hours"], [480, "8 hours"]];
const LONGER: [number, string][] = [[1440, "1 day"], [4320, "3 days"], [10080, "7 days"]];

const when = (iso: string) => new Date(iso).toLocaleString();
const scopeText = (window: MaintenanceWindow) =>
  window.scope === "site" ? "The whole site" : window.scope === "groups" ? `Groups: ${window.group_names.join(", ")}` : `Cameras: ${window.camera_names.join(", ")}`;

function WindowRow({ window, onEnd, ending }: { window: MaintenanceWindow; onEnd: (id: string) => void; ending: boolean }) {
  const open = window.state !== "ended";
  const until = window.ended_at ?? window.ends_at;
  return (
    <li className="maintenance-window" data-state={window.state}>
      <div>
        <strong>{window.name}</strong>
        <span className="tag">{window.state === "active" ? "Active" : window.state === "upcoming" ? "Upcoming" : "Ended"}</span>
        <p className="muted">{scopeText(window)}</p>
        <p className="muted">
          {window.state === "upcoming" ? `Starts ${when(window.starts_at)} · ` : window.state === "active" ? `Until ${when(until)} · ` : `${when(window.starts_at)} to ${when(until)} · `}
          {window.created_by ? `opened by ${window.created_by}` : "opened by someone since removed"}
        </p>
        {window.reason ? <p className="muted">{window.reason}</p> : null}
        {window.state !== "upcoming" ? (
          <p className="muted">Held back: {window.suppressed_alarms} alarm{window.suppressed_alarms === 1 ? "" : "s"} (kept in the inbox, marked) and {window.suppressed_incidents} incident notification{window.suppressed_incidents === 1 ? "" : "s"}.</p>
        ) : null}
      </div>
      {open ? (
        <button type="button" className="button" disabled={ending} onClick={() => onEnd(window.id)}>
          {window.state === "upcoming" ? `Cancel ${window.name}` : `End ${window.name} now`}
        </button>
      ) : null}
    </li>
  );
}

/** Maintenance windows: while cameras are being worked on they send no alerts. Anyone who can see cameras may open a short window on them. */
export function MaintenanceWorkspace() {
  const me = useMe();
  const administrator = me.data?.role === "administrator";
  const list = useMaintenance();
  const cameras = useCameras();
  const groups = useCameraGroups();
  const open = useOpenWindow();
  const end = useEndWindow();
  const [form, setForm] = useState({ name: "", reason: "", scope: "cameras" as "cameras" | "groups" | "site", camera_ids: [] as string[], group_ids: [] as string[], minutes: 60, later: "" });
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const toggle = (key: "camera_ids" | "group_ids", id: string) =>
    setForm(current => ({ ...current, [key]: current[key].includes(id) ? current[key].filter(item => item !== id) : [...current[key], id] }));

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    open.mutate(
      { name: form.name.trim(), reason: form.reason.trim(), scope: form.scope, camera_ids: form.scope === "cameras" ? form.camera_ids : [], group_ids: form.scope === "groups" ? form.group_ids : [],
        starts_at: form.later ? new Date(form.later).toISOString() : undefined, duration_minutes: form.minutes },
      {
        onSuccess: created => { setMessage(`${created.name} ${created.state === "upcoming" ? "is set" : "has started"}.`); setForm(current => ({ ...current, name: "", reason: "", camera_ids: [], group_ids: [], later: "" })); },
        onError: problem => setError(failureText(problem, "The maintenance window could not be opened.")),
      },
    );
  }

  const windows = list.data?.windows ?? [];
  const active = windows.filter(item => item.state === "active");
  const upcoming = windows.filter(item => item.state === "upcoming");
  const ended = windows.filter(item => item.state === "ended");
  const ready = form.name.trim() !== "" && (form.scope === "site" || (form.scope === "cameras" ? form.camera_ids.length > 0 : form.group_ids.length > 0));
  const durations = administrator ? [...DURATIONS, ...LONGER] : DURATIONS;
  return (
    <div className="maintenance">
      {list.data?.site_active ? <p className="notice" data-tone="warn" role="status">The whole site is in maintenance: no alerts are being sent.</p> : null}
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Start maintenance">
        <h2 className="field-heading">Start maintenance</h2>
        <p className="muted">While cameras are being worked on, nothing is sent about them: no push, pop-in, webhook, email or other rule action. Alarms are still kept in the inbox, marked as during maintenance. Recording carries on. A camera still faulty when the window ends is announced then.</p>
        <form onSubmit={submit} aria-label="Open a maintenance window">
          <label className="field"><span>What is being done</span>
            <input value={form.name} maxLength={120} placeholder="Cleaning the dock camera lens" onChange={event => setForm({ ...form, name: event.target.value })} />
          </label>
          <label className="field"><span>Notes (optional)</span>
            <input value={form.reason} maxLength={500} onChange={event => setForm({ ...form, reason: event.target.value })} />
          </label>
          {administrator ? (
            <label className="field"><span>Covers</span>
              <select value={form.scope} onChange={event => setForm({ ...form, scope: event.target.value as typeof form.scope })}>
                <option value="cameras">Chosen cameras</option>
                <option value="groups">Camera groups</option>
                <option value="site">The whole site</option>
              </select>
            </label>
          ) : null}
          {form.scope === "cameras" ? (
            <fieldset className="field-group"><legend>Cameras</legend>
              <ul className="check-list">
                {(cameras.data ?? []).filter(camera => !camera.archived_at).map(camera => (
                  <li key={camera.id}><label className="check"><input type="checkbox" checked={form.camera_ids.includes(camera.id)} onChange={() => toggle("camera_ids", camera.id)} /> {camera.name}</label></li>
                ))}
              </ul>
            </fieldset>
          ) : null}
          {form.scope === "groups" ? (
            <fieldset className="field-group"><legend>Camera groups</legend>
              <ul className="check-list">
                {(groups.data ?? []).map(group => (
                  <li key={group.id}><label className="check"><input type="checkbox" checked={form.group_ids.includes(group.id)} onChange={() => toggle("group_ids", group.id)} /> {group.name}</label></li>
                ))}
              </ul>
            </fieldset>
          ) : null}
          <label className="field"><span>For how long</span>
            <select value={form.minutes} onChange={event => setForm({ ...form, minutes: Number(event.target.value) })}>
              {durations.map(([minutes, label]) => <option key={minutes} value={minutes}>{label}</option>)}
            </select>
            <small className="muted field-hint">At most {administrator ? `${list.data?.limits.administrator_days ?? 7} days` : `${list.data?.limits.operator_hours ?? 8} hours`}. If the work goes on, end it and start another.</small>
          </label>
          <label className="field"><span>Starts (optional, now if empty)</span>
            <input type="datetime-local" value={form.later} onChange={event => setForm({ ...form, later: event.target.value })} />
          </label>
          <div className="field-actions">
            <button type="submit" className="button" disabled={!ready || open.isPending}>Start maintenance</button>
          </div>
        </form>
      </section>
      <section className="field-group settings-block" aria-label="Maintenance windows">
        <h2 className="field-heading">Windows</h2>
        {list.isPending ? <p className="muted">Loading…</p> : null}
        {list.data && !windows.length ? <p className="muted">No maintenance windows in the last 30 days.</p> : null}
        {([["Active", active], ["Upcoming", upcoming], ["Recent", ended]] as const).map(([title, items]) => items.length ? (
          <div key={title}>
            <h3>{title}</h3>
            <ul className="maintenance-list" aria-label={`${title} windows`}>
              {items.map(item => <WindowRow key={item.id} window={item} ending={end.isPending} onEnd={id => end.mutate(id, { onError: problem => setError(failureText(problem, "The window could not be ended.")) })} />)}
            </ul>
          </div>
        ) : null)}
      </section>
    </div>
  );
}
