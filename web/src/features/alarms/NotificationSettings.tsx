import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { currentSubscription, deviceLabel, forgetThisDevice, pushSupported } from "./pushDevice";

interface QuietHours { start: string; end: string; timezone: string }
interface Preferences {
  rule_ids: string[] | null;
  group_ids: string[] | null;
  quiet_hours: QuietHours | null;
  devices: { id: string; device_label: string; created_at: string; last_success_at: string | null; last_failure_at: string | null }[];
  rule_options: { id: string; name: string }[];
  group_options: { id: string; name: string }[];
}
type Choices = Pick<Preferences, "rule_ids" | "group_ids" | "quiet_hours">;

const key = ["push", "preferences"] as const;
const supported = pushSupported;

function toKey(base64: string): Uint8Array {
  const padded = base64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), char => char.charCodeAt(0));
}

/**
 * Push notifications for this person: turn them on or off for this device, choose the rules and
 * camera groups that notify, and set quiet hours. A notification opens its alarm here.
 */
export function NotificationSettings() {
  const client = useQueryClient();
  const display = useDisplaySettings();
  const preferences = useQuery({ queryKey: key, queryFn: ({ signal }) => api<Preferences>("/api/push/preferences", { signal }) });
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [draft, setDraft] = useState<Choices | null>(null);
  const [message, setMessage] = useState<{ tone: "good" | "bad"; text: string } | null>(null);

  useEffect(() => { void currentSubscription().then(found => setSubscribed(Boolean(found))).catch(() => setSubscribed(false)); }, []);
  useEffect(() => {
    if (preferences.data && !draft) setDraft({ rule_ids: preferences.data.rule_ids, group_ids: preferences.data.group_ids, quiet_hours: preferences.data.quiet_hours });
  }, [preferences.data, draft]);

  const device = useMutation({
    mutationFn: async (on: boolean) => {
      const existing = await currentSubscription();
      if (!on) {
        await forgetThisDevice();
        return false;
      }
      if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications are blocked for this site in the browser's settings.");
      const registration = await navigator.serviceWorker.ready;
      const { public_key } = await api<{ public_key: string }>("/api/push/public-key");
      const subscription = existing ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toKey(public_key) as BufferSource });
      const body = subscription.toJSON();
      await api("/api/push/subscriptions", { method: "POST", body: { endpoint: body.endpoint, keys: body.keys, device_label: deviceLabel() } });
      return true;
    },
    onSuccess: on => {
      setSubscribed(on);
      setMessage({ tone: "good", text: on ? "Notifications are on for this device." : "Notifications are off for this device." });
      void client.invalidateQueries({ queryKey: key });
    },
    onError: error => setMessage({ tone: "bad", text: error instanceof Error && !("status" in error) ? error.message : failureText(error) }),
  });

  const save = useMutation({
    mutationFn: (choices: Choices) => api<Preferences>("/api/push/preferences", { method: "PUT", body: choices }),
    onSuccess: saved => {
      client.setQueryData(key, (current: Preferences | undefined) => (current ? { ...current, ...saved } : current));
      setMessage({ tone: "good", text: "Notification choices saved." });
    },
    onError: error => setMessage({ tone: "bad", text: `Not saved: ${failureText(error)}.` }),
  });

  const dirty = useMemo(() => Boolean(draft && preferences.data && JSON.stringify(draft) !== JSON.stringify({
    rule_ids: preferences.data.rule_ids, group_ids: preferences.data.group_ids, quiet_hours: preferences.data.quiet_hours,
  })), [draft, preferences.data]);
  const toggle = (list: string[] | null, all: string[], id: string, on: boolean) => {
    const current = list ?? all;
    const next = on ? [...current, id] : current.filter(item => item !== id);
    return next.length === all.length && all.every(item => next.includes(item)) ? null : next;
  };

  const data = preferences.data;
  return (
    <details className="field-group notification-settings">
      <summary>Notification settings</summary>
      {!supported() ? <p className="muted field-hint">This browser cannot receive push notifications. Install the app, or use a current browser.</p> : (
        <div className="field-actions">
          <span>{subscribed === null ? "Checking this device…" : subscribed ? "Notifications are on for this device." : "Notifications are off for this device."}</span>
          <button type="button" className="button" disabled={subscribed === null || device.isPending} onClick={() => device.mutate(!subscribed)}>
            {subscribed ? "Turn off on this device" : "Turn on for this device"}
          </button>
        </div>
      )}
      {data && draft ? (
        <form className="notification-form" aria-label="Notification choices" onSubmit={event => { event.preventDefault(); save.mutate(draft); }}>
          <fieldset className="plain">
            <legend className="field-heading">Rules that notify me</legend>
            {data.rule_options.length ? (
              <ul className="choice-list member-list">
                {data.rule_options.map(rule => (
                  <li key={rule.id}><label><input type="checkbox" checked={draft.rule_ids === null || draft.rule_ids.includes(rule.id)}
                                                   onChange={event => setDraft({ ...draft, rule_ids: toggle(draft.rule_ids, data.rule_options.map(item => item.id), rule.id, event.target.checked) })} /> {rule.name}</label></li>
                ))}
              </ul>
            ) : <p className="muted field-hint">No rules are enabled yet.</p>}
          </fieldset>
          {data.group_options.length ? (
            <fieldset className="plain">
              <legend className="field-heading">Camera groups that notify me</legend>
              <ul className="choice-list member-list">
                {data.group_options.map(group => (
                  <li key={group.id}><label><input type="checkbox" checked={draft.group_ids === null || draft.group_ids.includes(group.id)}
                                                    onChange={event => setDraft({ ...draft, group_ids: toggle(draft.group_ids, data.group_options.map(item => item.id), group.id, event.target.checked) })} /> {group.name}</label></li>
                ))}
              </ul>
              <p className="muted field-hint">With every group ticked, cameras in no group notify too.</p>
            </fieldset>
          ) : null}
          <label className="check"><input type="checkbox" checked={draft.quiet_hours !== null}
                                          onChange={event => setDraft({ ...draft, quiet_hours: event.target.checked ? { start: "22:00", end: "07:00", timezone: display.data?.time_zone ?? "UTC" } : null })} /> Quiet hours</label>
          {draft.quiet_hours ? (
            <div className="field-row">
              <label className="field"><span>From</span><input type="time" value={draft.quiet_hours.start} onChange={event => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, start: event.target.value } })} /></label>
              <label className="field"><span>To</span><input type="time" value={draft.quiet_hours.end} onChange={event => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, end: event.target.value } })} /></label>
              <label className="field"><span>Time zone</span><input value={draft.quiet_hours.timezone} autoComplete="off" onChange={event => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, timezone: event.target.value } })} /></label>
            </div>
          ) : null}
          <div className="field-actions">
            <button type="submit" className="button" data-variant="primary" disabled={!dirty || save.isPending}>Save notification choices</button>
          </div>
          {data.devices.length ? <p className="muted field-hint">{data.devices.length} device{data.devices.length === 1 ? "" : "s"} receive your notifications.</p> : null}
        </form>
      ) : null}
      {message ? <p className="notice" data-tone={message.tone} role={message.tone === "good" ? "status" : "alert"}>{message.text}</p> : null}
    </details>
  );
}
