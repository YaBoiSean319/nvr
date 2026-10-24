import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { api } from "@/api/client";
import { Modal } from "@/components/Modal";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";
import { SecretDialog } from "./SecretDialog";

interface Webhook { id: string; name: string; url_origin: string; events: string[]; allow_private_receiver: boolean; enabled: boolean }
interface Delivery { id: string; endpoint_name: string; event_type: string; status: string; attempt_count: number; response_status: number | null; error_code: string | null; created_at: string }
interface Heartbeat {
  configured: boolean; enabled: boolean; url_origin: string | null; interval_seconds: number | null; allow_private_receiver?: boolean;
  last_attempt_at: string | null; last_success_at: string | null; last_http_status: number | null; last_error: string | null; checker_note?: string;
}

type Secret = { value: string; title: string; notice: string } | null;

/** Failure webhooks with signed deliveries, delivery history, and the outbound heartbeat. */
export function NotificationsSettings() {
  const client = useQueryClient();
  const webhooks = useQuery({ queryKey: ["webhooks"], queryFn: ({ signal }) => api<{ webhooks: Webhook[] }>("/api/webhooks", { signal }).then(result => result.webhooks) });
  const eventTypes = useQuery({ queryKey: ["webhooks", "events"], queryFn: ({ signal }) => api<{ event_types: string[] }>("/api/webhooks/event-types", { signal }).then(result => result.event_types) });
  const deliveries = useQuery({ queryKey: ["webhooks", "deliveries"], queryFn: ({ signal }) => api<{ deliveries: Delivery[] }>("/api/webhooks/deliveries?limit=25", { signal }).then(result => result.deliveries), refetchInterval: 10_000 });
  const [secret, setSecret] = useState<Secret>(null);
  const [message, setMessage] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const [form, setForm] = useState({ name: "", url: "", events: [] as string[], allow_private_receiver: false });
  const [removing, setRemoving] = useState<Webhook | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["webhooks"] });

  async function act(run: () => Promise<unknown>, success: string) {
    setMessage(null);
    try {
      await run();
      setMessage({ tone: "good", text: success });
      await refresh();
    } catch (error) {
      setMessage({ tone: "bad", text: failureText(error) });
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    await act(async () => {
      const result = await api<{ webhook: Webhook; signing_secret: string; secret_notice: string }>("/api/webhooks", { method: "POST", body: form });
      setSecret({ value: result.signing_secret, title: `Signing secret for ${result.webhook.name}`, notice: result.secret_notice });
      setForm({ name: "", url: "", events: [], allow_private_receiver: false });
    }, "Webhook added.");
  }

  return (
    <div className="settings-stack">
      <section className="field-group" aria-label="Failure webhooks">
        <h2 className="field-heading">Failure webhooks</h2>
        {message ? <p className="notice" data-tone={message.tone} role={message.tone === "good" ? "status" : "alert"}>{message.text}</p> : null}
        {webhooks.data?.length ? (
          <ul className="plain-list webhook-list">
            {webhooks.data.map(hook => (
              <li key={hook.id} aria-label={`Webhook ${hook.name}`}>
                <strong>{hook.name}</strong> <span className="mono">{hook.url_origin}</span> · {hook.enabled ? "enabled" : "paused"}
                <div className="muted field-hint">{hook.events.join(", ")}</div>
                <div className="field-actions">
                  <button type="button" className="button" onClick={() => void act(() => api(`/api/webhooks/${hook.id}/test`, { method: "POST" }), `Test event queued for ${hook.name}.`)}>Send test</button>
                  <button type="button" className="button" onClick={() => void act(() => api(`/api/webhooks/${hook.id}`, { method: "PATCH", body: { enabled: !hook.enabled } }), hook.enabled ? `${hook.name} paused.` : `${hook.name} enabled.`)}>{hook.enabled ? "Pause" : "Enable"}</button>
                  <button type="button" className="button" onClick={() => void act(async () => {
                    const result = await api<{ signing_secret: string; secret_notice: string }>(`/api/webhooks/${hook.id}/rotate-secret`, { method: "POST" });
                    setSecret({ value: result.signing_secret, title: `New signing secret for ${hook.name}`, notice: result.secret_notice });
                  }, "Signing secret rotated.")}>Rotate secret</button>
                  <button type="button" className="button" data-variant="danger" onClick={() => setRemoving(hook)}>Remove</button>
                </div>
              </li>
            ))}
          </ul>
        ) : <p className="muted">{webhooks.isPending ? "Loading…" : "No webhooks."}</p>}
        <form className="tab-form" aria-label="New webhook" onSubmit={event => void create(event)}>
          <div className="field-row">
            <label className="field"><span>Name</span><input value={form.name} maxLength={100} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
            <label className="field"><span>Receiver URL</span><input className="mono" value={form.url} placeholder="https://alerts.example/nvr" onChange={event => setForm({ ...form, url: event.target.value })} /></label>
          </div>
          <fieldset className="plain">
            <legend className="field-heading">Events</legend>
            <div className="check-grid">
              {(eventTypes.data ?? []).map(type => (
                <label key={type} className="check"><input type="checkbox" checked={form.events.includes(type)}
                       onChange={event => setForm({ ...form, events: event.target.checked ? [...form.events, type] : form.events.filter(item => item !== type) })} /> {type}</label>
              ))}
            </div>
          </fieldset>
          <label className="check"><input type="checkbox" checked={form.allow_private_receiver} onChange={event => setForm({ ...form, allow_private_receiver: event.target.checked })} /> Allow a receiver on a private network</label>
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={!form.name.trim() || !form.url.trim() || !form.events.length}>Add webhook</button></div>
        </form>
      </section>

      <section className="field-group" aria-label="Delivery history">
        <h2 className="field-heading">Delivery history</h2>
        {deliveries.data?.length ? (
          <table className="inventory-table">
            <thead><tr><th scope="col">When</th><th scope="col">Webhook</th><th scope="col">Event</th><th scope="col">Status</th><th scope="col">Attempts</th></tr></thead>
            <tbody>
              {deliveries.data.map(delivery => (
                <tr key={delivery.id} data-status={delivery.status}>
                  <td>{formatRelative(delivery.created_at)}</td><td>{delivery.endpoint_name}</td><td>{delivery.event_type}</td>
                  <td>{delivery.status}{delivery.response_status ? ` (HTTP ${delivery.response_status})` : ""}{delivery.error_code ? ` · ${delivery.error_code}` : ""}</td>
                  <td>{delivery.attempt_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">No deliveries yet.</p>}
      </section>

      <HeartbeatSection onSecret={setSecret} />
      <SecretDialog secret={secret?.value ?? null} title={secret?.title ?? ""} notice={secret?.notice ?? ""} onClose={() => setSecret(null)} />
      <Modal open={removing !== null} onOpenChange={open => !open && setRemoving(null)} title={`Remove ${removing?.name ?? "webhook"}?`}
             description="No further failure notifications are sent to this receiver."
             footer={<><button type="button" className="button" onClick={() => setRemoving(null)}>Cancel</button>
               <button type="button" className="button" data-variant="danger" onClick={() => { const hook = removing!; setRemoving(null); void act(() => api(`/api/webhooks/${hook.id}`, { method: "DELETE" }), `${hook.name} removed.`); }}>Remove webhook</button></>}>
        <p className="muted">Delivery history is kept.</p>
      </Modal>
    </div>
  );
}

function HeartbeatSection({ onSecret }: { onSecret: (secret: Secret) => void }) {
  const client = useQueryClient();
  const status = useQuery({ queryKey: ["heartbeat"], queryFn: ({ signal }) => api<Heartbeat>("/api/heartbeat", { signal }), refetchInterval: 15_000 });
  const published = useMemo(() => (status.data ? { target_url: "", interval_seconds: status.data.interval_seconds ?? 60, allow_private_receiver: Boolean(status.data.allow_private_receiver) } : undefined), [status.data]);
  const [message, setMessage] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const tab = useTabDraft(published, {
    label: "Heartbeat",
    editable: true,
    save: async (draft, base) => {
      try {
        const result = await api<{ heartbeat: Heartbeat; signing_secret: string | null; secret_notice: string | null }>("/api/heartbeat", { method: "PUT", body: draft });
        if (result.signing_secret) onSecret({ value: result.signing_secret, title: "Heartbeat signing secret", notice: result.secret_notice ?? "" });
        client.setQueryData(["heartbeat"], result.heartbeat);
        return { ok: true, base: { ...draft, target_url: "" }, outcome: { tone: "good", text: "Heartbeat saved." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Heartbeat not saved: ${failureText(error)}.` } };
      }
    },
  });

  async function act(run: () => Promise<unknown>, success: string) {
    setMessage(null);
    try {
      await run();
      setMessage({ tone: "good", text: success });
      await client.invalidateQueries({ queryKey: ["heartbeat"] });
    } catch (error) {
      setMessage({ tone: "bad", text: failureText(error) });
    }
  }

  const data = status.data;
  if (!data || !tab.draft) return <section className="field-group" aria-label="Heartbeat"><p className="muted">Loading heartbeat…</p></section>;
  return (
    <form className="field-group tab-form" aria-label="Heartbeat" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <h2 className="field-heading">External heartbeat</h2>
      <p>{data.configured && data.enabled
        ? `Sending to ${data.url_origin} every ${data.interval_seconds} s. Last success ${data.last_success_at ? formatRelative(data.last_success_at) : "never"}${data.last_error ? ` · last error: ${data.last_error}` : ""}.`
        : `Not configured${data.configured ? " (turned off)" : ""}. A heartbeat lets an outside checker notice when this NVR stops responding.`}</p>
      {data.checker_note ? <p className="muted field-hint">{data.checker_note}</p> : null}
      {message ? <p className="notice" data-tone={message.tone} role={message.tone === "good" ? "status" : "alert"}>{message.text}</p> : null}
      <div className="field-row">
        <label className="field"><span>Receiver URL</span><input className="mono" value={tab.draft.target_url} placeholder={data.configured && data.enabled ? "Saved (enter a URL to replace it)" : "https://checker.example/ping"} onChange={event => tab.change({ target_url: event.target.value })} /></label>
        <label className="field"><span>Interval (seconds)</span><input type="number" min={30} max={900} value={tab.draft.interval_seconds} onChange={event => tab.change({ interval_seconds: Number(event.target.value) })} /></label>
      </div>
      <label className="check"><input type="checkbox" checked={tab.draft.allow_private_receiver} onChange={event => tab.change({ allow_private_receiver: event.target.checked })} /> Allow a receiver on a private network</label>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={tab.draft.target_url.trim() ? null : "Enter the receiver URL to save."} />
      {data.configured && data.enabled ? (
        <div className="field-actions">
          <button type="button" className="button" onClick={() => void act(() => api("/api/heartbeat/test", { method: "POST" }), "Test heartbeat queued.")}>Send test</button>
          <button type="button" className="button" onClick={() => void act(async () => {
            const result = await api<{ signing_secret: string; secret_notice?: string }>("/api/heartbeat/rotate-secret", { method: "POST" });
            onSecret({ value: result.signing_secret, title: "New heartbeat signing secret", notice: result.secret_notice ?? "Update the receiver before the next heartbeat." });
          }, "Heartbeat secret rotated.")}>Rotate secret</button>
          <button type="button" className="button" data-variant="danger" onClick={() => void act(() => api("/api/heartbeat", { method: "DELETE" }), "Heartbeat removed.")}>Remove heartbeat</button>
        </div>
      ) : null}
    </form>
  );
}
