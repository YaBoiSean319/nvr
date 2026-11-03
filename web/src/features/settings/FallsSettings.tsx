import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface FallCamera { id: string; name: string; enabled: boolean; sensitivity: "low" | "normal" | "high"; stay_down_seconds: number }
interface FallAdmin {
  model: { installed: boolean; model: string; licence: string };
  defaults: { sensitivity: string; stay_down_seconds: number };
  cameras: FallCamera[];
  recent: { id: string; camera_id: string; camera_name: string; occurred_at: string; down_seconds: number }[];
}

const KEY = ["admin", "falls"] as const;
const SENSITIVITY: Record<FallCamera["sensitivity"], string> = {
  high: "High: catches more falls, and raises more alerts that are not falls",
  normal: "Normal",
  low: "Low: fewer false alerts, and misses more falls",
};

function CameraRow({ camera, modelInstalled, save, pending }: { camera: FallCamera; modelInstalled: boolean; save: (camera: FallCamera) => void; pending: boolean }) {
  const [seconds, setSeconds] = useState(String(camera.stay_down_seconds));
  const parsed = Number(seconds);
  const valid = Number.isFinite(parsed) && parsed >= 1 && parsed <= 60;
  return (
    <li>
      <label className="check">
        <input type="checkbox" checked={camera.enabled} disabled={pending || (!modelInstalled && !camera.enabled)}
               onChange={event => save({ ...camera, enabled: event.target.checked, stay_down_seconds: valid ? parsed : camera.stay_down_seconds })} /> Look for falls on {camera.name}
      </label>
      <div className="field-row">
        <label className="field"><span>Sensitivity for {camera.name}</span>
          <select value={camera.sensitivity} disabled={pending} onChange={event => save({ ...camera, sensitivity: event.target.value as FallCamera["sensitivity"], stay_down_seconds: valid ? parsed : camera.stay_down_seconds })}>
            {(Object.keys(SENSITIVITY) as FallCamera["sensitivity"][]).map(key => <option key={key} value={key}>{SENSITIVITY[key]}</option>)}
          </select>
        </label>
        <label className="field"><span>Seconds a person must stay down on {camera.name}</span>
          <input type="number" min={1} max={60} step={0.5} value={seconds} onChange={event => setSeconds(event.target.value)}
                 onBlur={() => { if (valid && parsed !== camera.stay_down_seconds) save({ ...camera, stay_down_seconds: parsed }); }} />
        </label>
      </div>
    </li>
  );
}

/** Which cameras look for people who have fallen, how sensitively, and the falls found; an alert needs a rule (Rules, "A person fell"). */
export function FallsSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<FallAdmin>("/api/admin/falls", { signal }), refetchInterval: 30_000 });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (camera: FallCamera) => api(`/api/admin/falls/cameras/${camera.id}`, { method: "PUT", body: { enabled: camera.enabled, sensitivity: camera.sensitivity, stay_down_seconds: camera.stay_down_seconds } }),
    onSuccess: async () => { setError(null); setMessage("Saved. The AI worker follows within about half a minute, with no restart."); await client.invalidateQueries({ queryKey: KEY }); },
    onError: problem => { setMessage(null); setError(failureText(problem, "The camera could not be saved.")); },
  });
  const data = state.data;
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Looking for falls">
        <h2 className="field-heading">Fall detection</h2>
        <p className="notice" data-tone="warn" role="note">
          This is an aid, not a safety system. It will miss some falls and it will raise alerts that are not falls, and it does not replace call buttons, regular checks or supervision.
          How well it works depends on the camera: it needs a view where people are large enough to see, not mostly hidden behind furniture. Try it on your own cameras and check what it finds before relying on it.
        </p>
        <p className="muted field-hint">
          On cameras you choose, the NVR works out the position of each person's shoulders, hips, knees and ankles about twice a second. A fall is a person who was upright, went low quickly and stayed low for the time set here.
          Someone first seen lying down (a resident in bed) is not a fall, and nor is someone who gets up again within the time. The default sensitivity is high, which leans towards catching falls.
          Turning it on only records falls: to be alerted, make a rule under Rules with the condition <strong>A person fell</strong>, and choose how staff are told (push, webhook and the other actions).
        </p>
        {data && !data.model.installed ? (
          <p className="notice" data-tone="warn" role="status">The pose model is not installed in this image, so falls cannot be looked for. See docs/fall-detection.md.</p>
        ) : null}
        <p className="muted field-hint">Model: {data?.model.model ?? "…"}. Licence: {data?.model.licence ?? "…"}.</p>
        <ul className="status-list" aria-label="Cameras for falls">
          {(data?.cameras ?? []).map(camera => <CameraRow key={`${camera.id}:${camera.enabled}:${camera.sensitivity}:${camera.stay_down_seconds}`} camera={camera} modelInstalled={Boolean(data?.model.installed)} save={value => save.mutate(value)} pending={save.isPending} />)}
          {data?.cameras.length === 0 ? <li className="muted">No cameras yet.</li> : null}
        </ul>
      </section>
      <section className="field-group settings-block" aria-label="Falls found">
        <h2 className="field-heading">Falls found</h2>
        <ul className="status-list" aria-label="Recent falls">
          {(data?.recent ?? []).map(item => (
            <li key={item.id}><strong>{item.camera_name}</strong><span className="muted">{new Date(item.occurred_at).toLocaleString()} · stayed down {item.down_seconds.toFixed(1)} s</span></li>
          ))}
          {data?.recent.length === 0 ? <li className="muted">No falls found yet.</li> : null}
        </ul>
      </section>
    </>
  );
}
