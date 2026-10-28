import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { useCameras } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";

interface Mapping { subject: string; types: string[]; camera_ids: string[] }
interface Source {
  id: string; name: string; kind: "http" | "mqtt" | "home_assistant"; enabled: boolean; target: string | null; topic: string | null; allow_private: boolean; has_secret: boolean;
  last_event_at: string | null; last_error: string | null; events: number; mappings: Mapping[];
}
interface Draft { id: string | null; kind: "http" | "mqtt"; name: string; enabled: boolean; secret: string; url: string; topic: string; allow_private: boolean; mappings: Mapping[]; saved: boolean }
interface SeenEvent { id: string; type: string; label: string; subject: string | null; occurred_at: string; mapped: boolean }
interface HomeAssistant { enabled: boolean; configured: boolean; broker: string | null; allow_private: boolean; discovery_prefix: string; node_id: string; events_topic: string }

const KEY = ["admin", "event-sources"] as const;
const blank = (): Draft => ({ id: null, kind: "http", name: "", enabled: true, secret: "", url: "", topic: "", allow_private: false, mappings: [], saved: false });

/** A random secret made in the browser: the NVR only ever stores it. */
function generateSecret(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
}

const splitList = (text: string) => text.split(",").map(item => item.trim()).filter(Boolean);

function SourceEditor({ draft, onChange, onSave, onCancel, saving }: { draft: Draft; onChange: (patch: Partial<Draft>) => void; onSave: () => void; onCancel: () => void; saving: boolean }) {
  const cameras = useCameras();
  const setMapping = (index: number, patch: Partial<Mapping>) => onChange({ mappings: draft.mappings.map((item, at) => (at === index ? { ...item, ...patch } : item)) });
  return (
    <fieldset className="field-group" aria-label="Event source">
      <div className="field-row">
        <label className="field"><span>Name</span><input value={draft.name} maxLength={120} onChange={event => onChange({ name: event.target.value })} /></label>
        <label className="field"><span>Receives events by</span>
          <select value={draft.kind} disabled={draft.saved} onChange={event => onChange({ kind: event.target.value as Draft["kind"] })}>
            <option value="http">Signed HTTP post</option><option value="mqtt">MQTT subscription</option>
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => onChange({ enabled: event.target.checked })} /> On</label>
      </div>
      {draft.kind === "http" ? (
        <div className="field-row">
          <label className="field"><span>Signing secret</span>
            <input className="mono" value={draft.secret} autoComplete="off" placeholder={draft.saved ? "Saved: leave blank to keep it" : "At least 16 characters"} onChange={event => onChange({ secret: event.target.value })} />
          </label>
          <button type="button" className="button" onClick={() => onChange({ secret: generateSecret() })}>Generate</button>
        </div>
      ) : (
        <>
          <label className="field"><span>Broker address</span>
            <input type="password" autoComplete="off" value={draft.url} placeholder={draft.saved ? "Saved: leave blank to keep it" : "mqtt://user:password@host:1883"} onChange={event => onChange({ url: event.target.value })} />
          </label>
          <label className="field"><span>Topic filter</span><input value={draft.topic} placeholder="building/doors/#" onChange={event => onChange({ topic: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={draft.allow_private} onChange={event => onChange({ allow_private: event.target.checked })} /> Broker is on a private network address</label>
        </>
      )}
      <h3 className="field-heading">Mappings to cameras</h3>
      <p className="muted field-hint">A mapping says which cameras an event is about, by the door, sensor or button it names. Use * for every subject.</p>
      {draft.mappings.map((mapping, index) => (
        <fieldset key={index} className="plain" aria-label={`Mapping ${index + 1}`}>
          <div className="field-row">
            <label className="field"><span>Subject</span><input value={mapping.subject} onChange={event => setMapping(index, { subject: event.target.value })} /></label>
            <label className="field"><span>Only these types (comma separated)</span><input value={mapping.types.join(", ")} onChange={event => setMapping(index, { types: splitList(event.target.value) })} /></label>
            <button type="button" className="button" data-variant="ghost" onClick={() => onChange({ mappings: draft.mappings.filter((_, at) => at !== index) })}>Remove mapping</button>
          </div>
          <ul className="check-list" aria-label="Cameras">
            {(cameras.data ?? []).map(camera => (
              <li key={camera.id}><label className="check"><input type="checkbox" checked={mapping.camera_ids.includes(camera.id)}
                onChange={event => setMapping(index, { camera_ids: event.target.checked ? [...mapping.camera_ids, camera.id] : mapping.camera_ids.filter(id => id !== camera.id) })} /> {camera.name}</label></li>
            ))}
          </ul>
        </fieldset>
      ))}
      <button type="button" className="button" onClick={() => onChange({ mappings: [...draft.mappings, { subject: "", types: [], camera_ids: [] }] })}>Add a mapping</button>
      <div className="field-actions">
        <button type="button" className="button" data-variant="primary" disabled={saving || !draft.name.trim()} onClick={onSave}>Save source</button>
        <button type="button" className="button" data-variant="ghost" onClick={onCancel}>Cancel</button>
      </div>
    </fieldset>
  );
}

function RecentEvents({ sourceId }: { sourceId: string }) {
  const events = useQuery({ queryKey: ["admin", "event-source-events", sourceId], queryFn: ({ signal }) => api<{ events: SeenEvent[] }>(`/api/event-sources/${sourceId}/events`, { signal }).then(result => result.events) });
  return (
    <ul className="status-list" aria-label="Recent events">
      {(events.data ?? []).map(item => (
        <li key={item.id}><span>{item.label}</span><span className="muted">{item.subject ?? "no subject"} · {new Date(item.occurred_at).toLocaleString()} · {item.mapped ? "mapped to a camera" : "not mapped"}</span></li>
      ))}
      {events.data?.length === 0 ? <li className="muted">Nothing received yet.</li> : null}
    </ul>
  );
}

function HomeAssistantBlock({ onMessage, onError }: { onMessage: (text: string) => void; onError: (text: string) => void }) {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: ["admin", "home-assistant"], queryFn: ({ signal }) => api<HomeAssistant>("/api/home-assistant", { signal }) });
  const [edits, setEdits] = useState<Partial<HomeAssistant & { url: string }>>({});
  const value = { enabled: false, allow_private: false, discovery_prefix: "homeassistant", node_id: "nvr", events_topic: "nvr/events", ...settings.data, ...edits };
  const save = useMutation({
    mutationFn: () => api<HomeAssistant>("/api/home-assistant", { method: "PUT", body: { enabled: value.enabled, allow_private: value.allow_private, discovery_prefix: value.discovery_prefix, node_id: value.node_id, events_topic: value.events_topic, ...(edits.url ? { url: edits.url } : {}) } }),
    onSuccess: async () => { onError(""); onMessage("Home Assistant settings saved."); setEdits({}); await client.invalidateQueries({ queryKey: ["admin", "home-assistant"] }); await client.invalidateQueries({ queryKey: KEY }); },
    onError: problem => onError(failureText(problem, "The Home Assistant settings could not be saved.")),
  });
  return (
    <section className="field-group settings-block" aria-label="Home Assistant">
      <h2 className="field-heading">Home Assistant</h2>
      <p className="muted field-hint">
        Publishes each camera's snapshot and alarm state to your MQTT broker as Home Assistant discovery messages, and accepts events from Home Assistant on the events topic (they appear
        as the source "Home Assistant", where you map them to cameras).
      </p>
      <label className="check"><input type="checkbox" checked={value.enabled} onChange={event => setEdits({ ...edits, enabled: event.target.checked })} /> Connect to Home Assistant's broker</label>
      <label className="field"><span>Broker address</span>
        <input type="password" autoComplete="off" value={edits.url ?? ""} placeholder={settings.data?.configured ? `Saved (${settings.data.broker}): leave blank to keep it` : "mqtt://user:password@host:1883"} onChange={event => setEdits({ ...edits, url: event.target.value })} />
      </label>
      <div className="field-row">
        <label className="field"><span>Discovery prefix</span><input value={value.discovery_prefix} onChange={event => setEdits({ ...edits, discovery_prefix: event.target.value })} /></label>
        <label className="field"><span>Node id</span><input value={value.node_id} onChange={event => setEdits({ ...edits, node_id: event.target.value })} /></label>
        <label className="field"><span>Events topic</span><input value={value.events_topic} onChange={event => setEdits({ ...edits, events_topic: event.target.value })} /></label>
      </div>
      <label className="check"><input type="checkbox" checked={value.allow_private} onChange={event => setEdits({ ...edits, allow_private: event.target.checked })} /> Broker is on a private network address</label>
      <div className="field-actions"><button type="button" className="button" data-variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>Save Home Assistant</button></div>
    </section>
  );
}

/** Outside events (door controllers, sensors, panic buttons) arriving by signed HTTP or MQTT, their mapping to cameras, and Home Assistant. */
export function EventSourcesSettings() {
  const client = useQueryClient();
  const sources = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<{ sources: Source[] }>("/api/event-sources", { signal }).then(result => result.sources) });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [shown, setShown] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const save = useMutation({
    mutationFn: (value: Draft) => {
      const body = {
        name: value.name, kind: value.kind, enabled: value.enabled, allow_private: value.allow_private, mappings: value.mappings.filter(item => item.subject.trim()),
        ...(value.secret ? { secret: value.secret } : {}), ...(value.url ? { url: value.url } : {}), ...(value.kind === "mqtt" ? { topic: value.topic } : {}),
      };
      return api<Source>(value.id ? `/api/event-sources/${value.id}` : "/api/event-sources", { method: value.id ? "PUT" : "POST", body });
    },
    onSuccess: async () => { setDraft(null); setError(null); setMessage("Saved."); await refresh(); },
    onError: problem => setError(failureText(problem, "The source could not be saved.")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/event-sources/${id}`, { method: "DELETE" }),
    onSuccess: async () => { setMessage("Deleted."); await refresh(); },
    onError: problem => setError(failureText(problem, "The source could not be deleted.")),
  });
  const edit = (source: Source) => {
    setMessage(null);
    setDraft({ id: source.id, kind: source.kind === "mqtt" ? "mqtt" : "http", name: source.name, enabled: source.enabled, secret: "", url: "", topic: source.topic ?? "", allow_private: source.allow_private, mappings: source.mappings, saved: true });
  };
  const inboundPath = (id: string) => `${window.location.origin}/api/inbound/events/${id}`;

  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Event sources">
        <h2 className="field-heading">Event sources</h2>
        <p className="muted field-hint">
          Door controllers, sensors and buttons send events here, and each event is tied to the cameras that watch the place it happened: it shows on the Playback timeline, can raise
          alarms through rules, and opens the matching clip. Secrets are kept encrypted and never shown again; see docs/external-events.md for the format and signing.
        </p>
        <ul className="status-list" aria-label="Sources">
          {(sources.data ?? []).map(source => (
            <li key={source.id}>
              <span className="row-select">{source.name}</span>
              <span className="muted">
                {source.kind === "http" ? `POST ${inboundPath(source.id)}` : source.kind === "mqtt" ? `MQTT ${source.target ?? ""} ${source.topic ?? ""}` : "From Home Assistant"} · {source.events} events{source.enabled ? "" : " · off"}
                {source.last_error ? ` · ${source.last_error}` : ""}
              </span>
              <span className="field-actions">
                {source.kind !== "home_assistant" ? <button type="button" className="button" onClick={() => edit(source)}>Edit {source.name}</button> : null}
                <button type="button" className="button" data-variant="ghost" onClick={() => setShown(shown === source.id ? null : source.id)}>Recent events</button>
                {source.kind !== "home_assistant" ? <button type="button" className="button" data-variant="ghost" onClick={() => remove.mutate(source.id)}>Delete {source.name}</button> : null}
              </span>
              {shown === source.id ? <RecentEvents sourceId={source.id} /> : null}
            </li>
          ))}
          {sources.data?.length === 0 ? <li className="muted">No sources yet.</li> : null}
        </ul>
        {draft ? (
          <SourceEditor draft={draft} saving={save.isPending} onChange={patch => setDraft(value => (value ? { ...value, ...patch } : value))} onSave={() => save.mutate(draft)} onCancel={() => setDraft(null)} />
        ) : <div className="field-actions"><button type="button" className="button" onClick={() => { setDraft(blank()); setMessage(null); }}>Add an event source</button></div>}
      </section>
      <HomeAssistantBlock onMessage={setMessage} onError={setError} />
    </>
  );
}
