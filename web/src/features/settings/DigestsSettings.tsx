import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { useCameraGroups } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";

type Kind = "webhook" | "slack" | "teams" | "mqtt";
interface ActionView { id: string; type: Kind; name: string; enabled: boolean; target: string | null; allow_private?: boolean; topic?: string; retain?: boolean; has_signing_secret?: boolean }
interface Schedule {
  id: string; name: string; enabled: boolean; scope: { site: boolean; group_ids?: string[]; camera_ids?: string[] }; recipient_user_id: number; recipient: string | null;
  shift: { days: number[]; start: string; end: string }; after_hours: { start: string; end: string }; summarise: boolean; last_shift_end: string | null; time_zone: string; actions: ActionView[];
}
interface Delivery { id: string; action_name: string; status: string; attempt_count: number; last_error: string | null; created_at: string }
interface ModelSettings { summary_enabled: boolean; configured: boolean; summary_model: string; allow_private: boolean }
interface ActionDraft { id?: string; type: Kind; name: string; enabled: boolean; url: string; signing_secret: string; allow_private: boolean; topic: string; retain: boolean; saved: boolean }
interface Draft { id: string | null; name: string; enabled: boolean; group: string; recipient: number | ""; days: number[]; start: string; end: string; afterStart: string; afterEnd: string; summarise: boolean; actions: ActionDraft[] }

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const KEY = ["admin", "digest-schedules"] as const;

function blank(): Draft {
  return { id: null, name: "", enabled: true, group: "", recipient: "", days: [0, 1, 2, 3, 4], start: "08:00", end: "16:00", afterStart: "22:00", afterEnd: "06:00", summarise: false, actions: [] };
}

function draftOf(schedule: Schedule): Draft {
  return {
    id: schedule.id, name: schedule.name, enabled: schedule.enabled, group: schedule.scope.group_ids?.[0] ?? "", recipient: schedule.recipient_user_id, days: schedule.shift.days,
    start: schedule.shift.start, end: schedule.shift.end, afterStart: schedule.after_hours.start, afterEnd: schedule.after_hours.end, summarise: schedule.summarise,
    actions: schedule.actions.map(action => ({ id: action.id, type: action.type, name: action.name, enabled: action.enabled, url: "", signing_secret: "", allow_private: Boolean(action.allow_private),
                                              topic: action.topic ?? "", retain: Boolean(action.retain), saved: true })),
  };
}

function bodyOf(draft: Draft) {
  return {
    name: draft.name, enabled: draft.enabled, scope: draft.group ? { site: false, group_ids: [draft.group] } : { site: true }, recipient_user_id: draft.recipient,
    shift: { days: draft.days, start: draft.start, end: draft.end }, after_hours: { start: draft.afterStart, end: draft.afterEnd }, summarise: draft.summarise,
    actions: draft.actions.map(action => ({
      id: action.id, type: action.type, name: action.name, enabled: action.enabled, allow_private: action.allow_private, retain: action.retain,
      ...(action.url ? { url: action.url } : {}), ...(action.signing_secret ? { signing_secret: action.signing_secret } : {}), ...(action.type === "mqtt" ? { topic: action.topic } : {}),
    })),
  };
}

function ActionFields({ action, onChange, onRemove }: { action: ActionDraft; onChange: (patch: Partial<ActionDraft>) => void; onRemove: () => void }) {
  return (
    <fieldset className="field-group" aria-label={`Action ${action.name || action.type}`}>
      <div className="field-actions">
        <label className="field"><span>Delivered by</span>
          <select value={action.type} onChange={event => onChange({ type: event.target.value as Kind })}>
            <option value="webhook">Signed webhook</option><option value="slack">Slack</option><option value="teams">Microsoft Teams</option><option value="mqtt">MQTT</option>
          </select>
        </label>
        <label className="field"><span>Name</span><input value={action.name} maxLength={80} onChange={event => onChange({ name: event.target.value })} /></label>
        <label className="check"><input type="checkbox" checked={action.enabled} onChange={event => onChange({ enabled: event.target.checked })} /> On</label>
        <button type="button" className="button" data-variant="ghost" onClick={onRemove}>Remove</button>
      </div>
      <label className="field"><span>{action.type === "mqtt" ? "Broker address" : "Destination address"}</span>
        <input value={action.url} type="password" autoComplete="off" placeholder={action.saved ? "Saved: leave blank to keep it" : ""} onChange={event => onChange({ url: event.target.value })} />
      </label>
      {action.type === "webhook" ? (
        <label className="field"><span>Signing secret</span>
          <input value={action.signing_secret} type="password" autoComplete="off" placeholder={action.saved ? "Saved: leave blank to keep it" : "At least 16 characters"} onChange={event => onChange({ signing_secret: event.target.value })} />
        </label>
      ) : null}
      {action.type === "mqtt" ? <label className="field"><span>Topic</span><input value={action.topic} onChange={event => onChange({ topic: event.target.value })} /></label> : null}
      {action.type === "webhook" || action.type === "mqtt" ? (
        <label className="check"><input type="checkbox" checked={action.allow_private} onChange={event => onChange({ allow_private: event.target.checked })} /> Receiver is on a private network address</label>
      ) : null}
    </fieldset>
  );
}

/** Digest schedules (who gets which digest at the end of which shift, and through which actions) and the optional summarising model. */
export function DigestsSettings() {
  const client = useQueryClient();
  const groups = useCameraGroups();
  const users = useQuery({ queryKey: ["admin", "users-for-digests"], queryFn: ({ signal }) => api<{ users: { id: number; username: string }[] }>("/api/admin/users", { signal }).then(result => result.users) });
  const schedules = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<{ schedules: Schedule[] }>("/api/digests/schedules", { signal }).then(result => result.schedules) });
  const model = useQuery({ queryKey: ["admin", "digest-model"], queryFn: ({ signal }) => api<ModelSettings>("/api/digests/settings", { signal }) });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shownDeliveries, setShownDeliveries] = useState<string | null>(null);
  const deliveries = useQuery({
    queryKey: ["admin", "digest-deliveries", shownDeliveries], enabled: shownDeliveries !== null,
    queryFn: ({ signal }) => api<{ deliveries: Delivery[] }>(`/api/digests/schedules/${shownDeliveries}/deliveries`, { signal }).then(result => result.deliveries),
  });
  const [edits, setEdits] = useState<{ enabled?: boolean; url?: string; model?: string; allow_private?: boolean }>({});
  const modelDraft = { enabled: edits.enabled ?? model.data?.summary_enabled ?? false, url: edits.url ?? "", model: edits.model ?? model.data?.summary_model ?? "", allow_private: edits.allow_private ?? model.data?.allow_private ?? false };
  const setModelDraft = (value: typeof modelDraft) => setEdits(value);

  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const save = useMutation({
    mutationFn: (value: Draft) => api<Schedule>(value.id ? `/api/digests/schedules/${value.id}` : "/api/digests/schedules", { method: value.id ? "PUT" : "POST", body: bodyOf(value) }),
    onSuccess: async () => { setDraft(null); setError(null); setMessage("Saved."); await refresh(); },
    onError: problem => setError(failureText(problem, "The schedule could not be saved.")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/digests/schedules/${id}`, { method: "DELETE" }),
    onSuccess: async () => { setMessage("Deleted."); await refresh(); },
    onError: problem => setError(failureText(problem, "The schedule could not be deleted.")),
  });
  const run = useMutation({
    mutationFn: (id: string) => api<{ digest_id: string; already_made: boolean }>(`/api/digests/schedules/${id}/run`, { method: "POST" }),
    onSuccess: async result => { setError(null); setMessage(result.already_made ? "The last shift's digest was already made." : "Made the last shift's digest and queued it for delivery."); await client.invalidateQueries({ queryKey: ["digests"] }); },
    onError: problem => setError(failureText(problem, "The digest could not be made.")),
  });
  const saveModel = useMutation({
    mutationFn: () => api<ModelSettings>("/api/digests/settings", { method: "PUT", body: { ...modelDraft, url: modelDraft.url || null } }),
    onSuccess: async () => { setError(null); setMessage("Model settings saved."); setEdits({}); await client.invalidateQueries({ queryKey: ["admin", "digest-model"] }); },
    onError: problem => setError(failureText(problem, "The model settings could not be saved.")),
  });

  const patch = (change: Partial<Draft>) => setDraft(value => (value ? { ...value, ...change } : value));
  const valid = draft !== null && draft.name.trim() && draft.recipient !== "" && draft.days.length > 0;

  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Digest schedules">
        <h2 className="field-heading">Digest schedules</h2>
        <p className="muted field-hint">
          A digest is a written account of a shift: alarms by rule and outcome, people after hours, vehicles at gates, recording gaps and system incidents, each with a link. It is made
          from stored data when the shift ends, for one person's view of the cameras (it never lists one they may not see), and delivered through the actions you choose.
        </p>
        <ul className="status-list" aria-label="Schedules">
          {(schedules.data ?? []).map(item => (
            <li key={item.id}>
              <span className="row-select">{item.name}</span>
              <span className="muted">for {item.recipient ?? "removed account"} · {item.scope.site ? "whole site" : "chosen cameras"} · shifts end {item.shift.end} ({item.time_zone}) on {item.shift.days.map(day => DAYS[day]).join(", ")}{item.enabled ? "" : " · off"}</span>
              <span className="field-actions">
                <button type="button" className="button" onClick={() => { setDraft(draftOf(item)); setMessage(null); }}>Edit {item.name}</button>
                <button type="button" className="button" onClick={() => run.mutate(item.id)} disabled={run.isPending}>Make last shift's digest now</button>
                <button type="button" className="button" data-variant="ghost" onClick={() => setShownDeliveries(shownDeliveries === item.id ? null : item.id)}>Deliveries</button>
              </span>
              {shownDeliveries === item.id ? (
                <table className="inventory-table" aria-label={`Deliveries of ${item.name}`}>
                  <thead><tr><th scope="col">Action</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Last problem</th></tr></thead>
                  <tbody>{(deliveries.data ?? []).map(entry => <tr key={entry.id}><td>{entry.action_name}</td><td>{entry.status}</td><td>{entry.attempt_count}</td><td>{entry.last_error ?? ""}</td></tr>)}</tbody>
                </table>
              ) : null}
            </li>
          ))}
        </ul>
        {schedules.data && !schedules.data.length ? <p className="muted">No schedules yet.</p> : null}
        {!draft ? <button type="button" className="button" onClick={() => { setDraft(blank()); setMessage(null); }}>New schedule</button> : (
          <form className="tab-form" aria-label={draft.id ? "Edit schedule" : "New schedule"} onSubmit={event => { event.preventDefault(); if (valid) save.mutate(draft); }}>
            <label className="field"><span>Name</span><input value={draft.name} maxLength={120} onChange={event => patch({ name: event.target.value })} required /></label>
            <label className="field"><span>For</span>
              <select value={draft.recipient} onChange={event => patch({ recipient: event.target.value ? Number(event.target.value) : "" })} required>
                <option value="">Choose a person</option>{(users.data ?? []).map(user => <option key={user.id} value={user.id}>{user.username}</option>)}
              </select>
            </label>
            <label className="field"><span>Cover</span>
              <select value={draft.group} onChange={event => patch({ group: event.target.value })}>
                <option value="">The whole site</option>{(groups.data ?? []).map(group => <option key={group.id} value={group.id}>{group.name}</option>)}
              </select>
            </label>
            <fieldset className="plain"><legend className="field-heading">A shift ends on</legend>
              {DAYS.map((label, day) => (
                <label key={label} className="check"><input type="checkbox" checked={draft.days.includes(day)} onChange={event => patch({ days: event.target.checked ? [...draft.days, day].sort() : draft.days.filter(item => item !== day) })} /> {label}</label>
              ))}
            </fieldset>
            <div className="field-actions">
              <label className="field"><span>Shift starts</span><input type="time" value={draft.start} onChange={event => patch({ start: event.target.value })} required /></label>
              <label className="field"><span>Shift ends</span><input type="time" value={draft.end} onChange={event => patch({ end: event.target.value })} required /></label>
              <label className="field"><span>After hours from</span><input type="time" value={draft.afterStart} onChange={event => patch({ afterStart: event.target.value })} required /></label>
              <label className="field"><span>After hours until</span><input type="time" value={draft.afterEnd} onChange={event => patch({ afterEnd: event.target.value })} required /></label>
            </div>
            <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => patch({ enabled: event.target.checked })} /> Make this digest</label>
            <label className="check"><input type="checkbox" checked={draft.summarise} disabled={!model.data?.summary_enabled} onChange={event => patch({ summarise: event.target.checked })} /> Add a summary paragraph from the local model{model.data?.summary_enabled ? "" : " (not configured)"}</label>
            <h3 className="field-heading">Delivered through</h3>
            {draft.actions.map((action, index) => (
              <ActionFields key={action.id ?? index} action={action} onRemove={() => patch({ actions: draft.actions.filter((_, position) => position !== index) })}
                            onChange={change => patch({ actions: draft.actions.map((item, position) => (position === index ? { ...item, ...change } : item)) })} />
            ))}
            <button type="button" className="button" onClick={() => patch({ actions: [...draft.actions, { type: "webhook", name: "", enabled: true, url: "", signing_secret: "", allow_private: false, topic: "", retain: false, saved: false }] })}>Add an action</button>
            <div className="field-actions">
              <button type="submit" className="button" data-variant="primary" disabled={!valid || save.isPending}>Save schedule</button>
              <button type="button" className="button" onClick={() => setDraft(null)}>Cancel</button>
              {draft.id ? <button type="button" className="button" data-variant="danger" onClick={() => { if (window.confirm(`Delete the schedule “${draft.name}”? Its digests are kept.`)) { remove.mutate(draft.id!); setDraft(null); } }}>Delete schedule</button> : null}
            </div>
          </form>
        )}
      </section>
      <section className="field-group settings-block" aria-label="Summarising model">
        <h2 className="field-heading">Summarising model (optional)</h2>
        <p className="muted field-hint">
          Off by default. While it is off, or has no address, digests are complete without it and nothing is sent anywhere. When on, each scheduled digest that asks for a summary sends its text to this
          OpenAI-compatible endpoint, which you run, and adds the one paragraph it returns, labelled as written by the model. Use a private address only for a model on your own network.
        </p>
        <form className="tab-form" onSubmit={event => { event.preventDefault(); saveModel.mutate(); }}>
          <label className="field"><span>Chat completions address</span>
            <input type="password" autoComplete="off" value={modelDraft.url} placeholder={model.data?.configured ? "Saved: leave blank to keep it" : "https://…/v1/chat/completions"} onChange={event => setModelDraft({ ...modelDraft, url: event.target.value })} />
          </label>
          <label className="field"><span>Model name</span><input value={modelDraft.model} onChange={event => setModelDraft({ ...modelDraft, model: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={modelDraft.allow_private} onChange={event => setModelDraft({ ...modelDraft, allow_private: event.target.checked })} /> The endpoint is on a private network address</label>
          <label className="check"><input type="checkbox" checked={modelDraft.enabled} onChange={event => setModelDraft({ ...modelDraft, enabled: event.target.checked })} /> Turn summaries on</label>
          <button type="submit" className="button" disabled={saveModel.isPending}>Save model settings</button>
        </form>
      </section>
    </>
  );
}
