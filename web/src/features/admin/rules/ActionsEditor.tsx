import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Camera } from "@/api/types";
import { failureText } from "../tabDraft";
import { ACTION_LABEL, listDeliveries, newAction, type ActionDraft, type ActionType } from "./api";

const MAX_ACTIONS = 10;
const TYPES = Object.keys(ACTION_LABEL) as ActionType[];
const STATUS_LABEL = { queued: "Waiting to retry", sending: "Sending", delivered: "Delivered", failed: "Failed" } as const;

function SecretField({ label, saved, value, onChange, hint }: { label: string; saved: boolean; value: string | undefined; onChange: (value: string) => void; hint?: string }) {
  return (
    <label className="field"><span>{label}</span>
      <input type="password" autoComplete="off" value={value ?? ""} placeholder={saved ? "Saved — leave empty to keep" : ""} onChange={event => onChange(event.target.value)} />
      {hint ? <small className="muted field-hint">{hint}</small> : null}
    </label>
  );
}

function ActionFields({ action, cameras, onChange }: { action: ActionDraft; cameras: Camera[]; onChange: (next: Partial<ActionDraft>) => void }) {
  const saved = Boolean(action.id && action.target);
  switch (action.type) {
    case "webhook":
      return (
        <>
          <SecretField label="Receiver URL" saved={saved} value={action.url} onChange={url => onChange({ url })} hint="Public HTTPS, or an authorised private IP address." />
          <SecretField label="Signing secret" saved={Boolean(action.has_signing_secret)} value={action.signing_secret} onChange={signing_secret => onChange({ signing_secret })}
                       hint="At least 16 characters. Requests are signed like every NVR webhook (X-NVR-Signature)." />
          <label className="check"><input type="checkbox" checked={Boolean(action.allow_private)} onChange={event => onChange({ allow_private: event.target.checked })} /> Allow a private IP address</label>
        </>
      );
    case "mqtt":
      return (
        <>
          <SecretField label="Broker" saved={saved} value={action.url} onChange={url => onChange({ url })}
                       hint="mqtts://user:password@host:8883, or mqtt:// for an authorised private IP address." />
          <label className="field"><span>Topic</span><input value={action.topic ?? ""} onChange={event => onChange({ topic: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={Boolean(action.retain)} onChange={event => onChange({ retain: event.target.checked })} /> Retain the message</label>
          <label className="check"><input type="checkbox" checked={Boolean(action.allow_private)} onChange={event => onChange({ allow_private: event.target.checked })} /> Allow a private IP address</label>
        </>
      );
    case "slack":
    case "teams":
      return <SecretField label="Incoming webhook URL" saved={saved} value={action.url} onChange={url => onChange({ url })}
                          hint={`The message names the rule, object, camera and time, with snapshot and alarm links when the NVR's public address is set.`} />;
    case "relay":
      return (
        <>
          <label className="field"><span>Camera</span>
            <select value={action.camera_id ?? ""} onChange={event => onChange({ camera_id: event.target.value || null })}>
              <option value="">Choose a camera…</option>
              {cameras.filter(camera => camera.device_service_url || camera.id === action.camera_id).map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Relay output token (optional)</span>
            <input value={action.relay_token ?? ""} placeholder="The camera's first relay" onChange={event => onChange({ relay_token: event.target.value || null })} />
          </label>
          <label className="field"><span>Hold for (seconds)</span>
            <input type="number" min={0.1} max={60} step={0.1} value={Number.isNaN(action.duration_seconds) ? "" : action.duration_seconds ?? 1}
                   onChange={event => onChange({ duration_seconds: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
          </label>
        </>
      );
  }
}

/** What a rule does besides raising an alarm. Saved secrets are never shown again. */
export function ActionsEditor({ actions, cameras, onChange }: { actions: ActionDraft[]; cameras: Camera[]; onChange: (actions: ActionDraft[]) => void }) {
  const [adding, setAdding] = useState<ActionType | "">("");
  const update = (index: number, next: Partial<ActionDraft>) => onChange(actions.map((action, at) => (at === index ? { ...action, ...next } : action)));
  return (
    <fieldset className="field-group">
      <legend>Actions</legend>
      <p className="muted field-hint">Each event also runs these, from the background worker, with retries. Test mode lists them without running them.</p>
      {actions.map((action, index) => (
        <section key={action.id ?? action.key ?? index} className="rule-action" aria-label={`Action: ${action.name || ACTION_LABEL[action.type]}`}>
          <header className="rule-action-head">
            <strong>{ACTION_LABEL[action.type]}</strong>
            {action.target ? <span className="muted mono">{action.target}</span> : null}
            <button type="button" className="button" onClick={() => onChange(actions.filter((_, at) => at !== index))} aria-label={`Remove ${action.name}`}>Remove</button>
          </header>
          <label className="field"><span>Name</span><input value={action.name} maxLength={80} onChange={event => update(index, { name: event.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={action.enabled} onChange={event => update(index, { enabled: event.target.checked })} /> Enabled</label>
          <ActionFields action={action} cameras={cameras} onChange={next => update(index, next)} />
        </section>
      ))}
      <label className="field"><span>Add an action</span>
        <select value={adding} disabled={actions.length >= MAX_ACTIONS}
                onChange={event => {
                  const type = event.target.value as ActionType | "";
                  setAdding("");
                  if (type) onChange([...actions, newAction(type)]);
                }}>
          <option value="">{actions.length >= MAX_ACTIONS ? `Up to ${MAX_ACTIONS} actions` : "Choose an action…"}</option>
          {TYPES.map(type => <option key={type} value={type}>{ACTION_LABEL[type]}</option>)}
        </select>
      </label>
    </fieldset>
  );
}

/** The rule's recent action deliveries. */
export function DeliveryLog({ ruleId, when }: { ruleId: string; when: (iso: string) => string }) {
  const deliveries = useQuery({ queryKey: ["rules", ruleId, "deliveries"], queryFn: ({ signal }) => listDeliveries(ruleId, signal), refetchInterval: 5_000 });
  return (
    <section className="field-group" aria-label="Action deliveries">
      <h3 className="field-heading">Action deliveries</h3>
      {deliveries.error ? <p className="notice" data-tone="bad" role="alert">{failureText(deliveries.error, "Deliveries could not be loaded.")}</p> : null}
      {deliveries.data && !deliveries.data.length ? <p className="muted field-hint">Nothing delivered yet.</p> : null}
      {deliveries.data?.length ? (
        <table className="zone-table rule-test-table">
          <thead><tr><th scope="col">When</th><th scope="col">Action</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Last error</th></tr></thead>
          <tbody>
            {deliveries.data.map(delivery => (
              <tr key={delivery.id} data-status={delivery.status}>
                <td>{when(delivery.created_at)}</td>
                <td>{delivery.action_name}</td>
                <td>{STATUS_LABEL[delivery.status]}{delivery.status === "queued" && delivery.attempt_count ? ` (next ${when(delivery.next_attempt_at)})` : ""}</td>
                <td>{delivery.attempt_count}</td>
                <td className="mono">{delivery.last_error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
