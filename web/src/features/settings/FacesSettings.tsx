import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

export interface FaceEntry { id: string; name: string; reason: string; expires_at: string | null; expired: boolean; enabled: boolean; embeddings: number; has_picture: boolean; created_by: string | null; created_at: string }
interface FaceStatus { models: { available: boolean; detector_installed: boolean; recogniser_installed: boolean }; match_threshold: number; default_threshold: number; cameras_enabled: number; entries: number }
interface FaceCameras { cameras: { camera_id: string; name: string; enabled: boolean }[]; models_available: boolean; unalerted_camera_ids: string[] }
interface Sighting { id: string; entry_id: string; entry_name: string; camera_id: string; camera_name: string; seen_at: string; similarity: number; has_picture: boolean }

export const FACES_KEY = ["admin", "faces"] as const;
export const useFaceEntries = () => useQuery({ queryKey: [...FACES_KEY, "watch"], queryFn: ({ signal }) => api<{ entries: FaceEntry[]; match_threshold: number }>("/api/faces/watch", { signal }), retry: false });

function Entry({ entry, save, remove, pending }: { entry: FaceEntry; save: (body: Record<string, unknown>) => void; remove: () => void; pending: boolean }) {
  const [confirming, setConfirming] = useState(false);
  const state = !entry.enabled ? "switched off" : entry.expired ? "expired" : "watching";
  return (
    <li className="face-entry" aria-label={`Watched face ${entry.name}`}>
      {entry.has_picture ? <img src={`/api/faces/watch/${entry.id}/picture`} alt="" width={64} height={64} className="face-thumb" /> : <span className="face-thumb" aria-hidden="true" />}
      <div>
        <strong>{entry.name}</strong> <span className="muted">({state})</span>
        <p className="muted">{entry.reason}</p>
        <p className="muted">{entry.embeddings} {entry.embeddings === 1 ? "picture" : "pictures"} · added {new Date(entry.created_at).toLocaleDateString()}{entry.created_by ? ` by ${entry.created_by}` : ""}
          {entry.expires_at ? ` · ${entry.expired ? "expired" : "expires"} ${new Date(entry.expires_at).toLocaleDateString()}` : " · no expiry"}</p>
        <div className="field-actions">
          <label className="check"><input type="checkbox" checked={entry.enabled} disabled={pending} onChange={event => save({ enabled: event.target.checked })} /> Look for this person</label>
          {entry.expires_at ? <button type="button" className="button" disabled={pending} onClick={() => save({ clear_expiry: true })}>Remove the expiry</button> : null}
          {confirming ? (
            <>
              <button type="button" className="button" data-variant="danger" disabled={pending} onClick={remove}>Yes, delete {entry.name} and their pictures</button>
              <button type="button" className="button" onClick={() => setConfirming(false)}>Keep</button>
            </>
          ) : <button type="button" className="button" data-variant="ghost" onClick={() => setConfirming(true)}>Delete…</button>}
        </div>
      </div>
    </li>
  );
}

/** The face watch list, which cameras look for those faces, how alike a face must be, and the matches found; an alert needs a rule (Rules, "A watched face is seen"). A face is added from a recording, in Playback, "Find a face…". */
export function FacesSettings() {
  const client = useQueryClient();
  const status = useQuery({ queryKey: [...FACES_KEY, "status"], queryFn: ({ signal }) => api<FaceStatus>("/api/faces/status", { signal }) });
  const watch = useFaceEntries();
  const cameras = useQuery({ queryKey: [...FACES_KEY, "cameras"], queryFn: ({ signal }) => api<FaceCameras>("/api/faces/cameras", { signal }) });
  const [showSightings, setShowSightings] = useState(false);
  // Looking at matches is audited, so they are fetched when the person asks (the button, Refresh) and never by a save elsewhere on the page, a return to the window or a redraw (#306).
  const sightings = useQuery({ queryKey: ["face-sightings"], enabled: showSightings, staleTime: Infinity, gcTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false, queryFn: ({ signal }) => api<{ sightings: Sighting[] }>("/api/faces/sightings", { signal }) });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [threshold, setThreshold] = useState<number | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: FACES_KEY });
  const failed = (fallback: string) => (problem: unknown) => { setMessage(null); setError(failureText(problem, fallback)); };

  const switchCamera = useMutation({
    mutationFn: (value: { id: string; enabled: boolean }) => api(`/api/faces/cameras/${value.id}`, { method: "PUT", body: { enabled: value.enabled } }),
    onSuccess: async () => { setError(null); setMessage("Saved. The AI worker follows within about half a minute, with no restart."); await refresh(); },
    onError: failed("The camera could not be saved."),
  });
  const saveThreshold = useMutation({
    mutationFn: (value: number) => api("/api/faces/settings", { method: "PUT", body: { match_threshold: value } }),
    onSuccess: async () => { setError(null); setMessage("Saved."); setThreshold(null); await refresh(); },
    onError: failed("The setting could not be saved."),
  });
  const updateEntry = useMutation({
    mutationFn: (value: { id: string; body: Record<string, unknown> }) => api(`/api/faces/watch/${value.id}`, { method: "PATCH", body: value.body }),
    onSuccess: async () => { setError(null); setMessage("Saved."); await refresh(); },
    onError: failed("The entry could not be saved."),
  });
  const removeEntry = useMutation({
    mutationFn: (id: string) => api(`/api/faces/watch/${id}`, { method: "DELETE" }),
    onSuccess: async () => { setError(null); setMessage("Deleted, with their pictures and the numbers that describe their face."); await refresh(); },
    onError: failed("The entry could not be deleted."),
  });
  const making = useRef(false);  // Set at once, where a disabled button only follows the next render: two quick clicks must make one rule
  const createRule = useMutation({
    mutationFn: (cameraIds: string[]) => api("/api/rules", { method: "POST", body: { name: "Watched face seen", camera_ids: cameraIds, condition: { type: "face", entry_ids: [] }, cooldown_seconds: 300, pop_into_live: true } }),
    // The notice goes away only when the page has heard which cameras the new rule covers, so a second click cannot make a second rule.
    onSuccess: async () => { setError(null); setMessage("Rule created: a watched face now raises an alarm and comes up in Live for people who opted in. Add push or other actions to it under Rules."); await refresh(); },
    onError: failed("The rule could not be created."),
    onSettled: () => { making.current = false; },
  });
  const makeRule = (cameraIds: string[]) => { if (making.current) return; making.current = true; createRule.mutate(cameraIds); };

  const models = status.data?.models;
  const current = threshold ?? status.data?.match_threshold ?? 0.45;
  const uncovered = cameras.data?.unalerted_camera_ids ?? [];
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Looking for faces">
        <h2 className="field-heading">Face watch</h2>
        <p className="notice" data-tone="warn" role="note">
          This looks for the faces of specific people someone chose to watch for, such as a person who has been trespassed. It is an aid, not proof: it will miss some faces and it can match the wrong person, so check every alert before acting on it.
          Face recognition of people on your premises may be regulated where you are; make sure you are allowed to use it and that people are told. See docs/face-watch.md.
        </p>
        <p className="muted field-hint">
          Nothing looks for a face until you switch a camera on below, and only faces on the list below are ever kept: every other face is discarded the moment it is compared.
          A face is added from a recording: open Playback, go to a moment, and choose <strong>Find a face…</strong>. Faces behind a privacy mask are never read. Everything here is for administrators and is recorded in the audit log.
        </p>
        {models && !models.available ? <p className="notice" data-tone="warn" role="status">The face models are not installed in this image, so no face can be added or recognized. See docs/face-watch.md.</p> : null}
        <label className="field"><span>How alike a face must be to count as a match: {current.toFixed(2)}</span>
          <input type="range" min={0.25} max={0.9} step={0.01} value={current} aria-label="Match threshold" onChange={event => setThreshold(Number(event.target.value))} />
          <small className="muted field-hint">Lower finds more returns and raises more wrong matches; higher is stricter and misses more. The default is {(status.data?.default_threshold ?? 0.45).toFixed(2)}.</small>
        </label>
        <div className="field-actions">
          <button type="button" className="button" disabled={threshold === null || saveThreshold.isPending} onClick={() => threshold !== null && saveThreshold.mutate(threshold)}>Save threshold</button>
        </div>
      </section>
      <section className="field-group settings-block" aria-label="Cameras for faces">
        <h2 className="field-heading">Cameras</h2>
        <ul className="status-list" aria-label="Cameras that look for watched faces">
          {(cameras.data?.cameras ?? []).map(camera => (
            <li key={camera.camera_id}>
              <label className="check"><input type="checkbox" checked={camera.enabled} disabled={switchCamera.isPending} onChange={event => switchCamera.mutate({ id: camera.camera_id, enabled: event.target.checked })} /> Look for watched faces on {camera.name}</label>
            </li>
          ))}
          {cameras.data?.cameras.length === 0 ? <li className="muted">No cameras yet.</li> : null}
        </ul>
        {uncovered.length > 0 && (watch.data?.entries.length ?? 0) > 0 ? (
          <div className="notice" data-tone="warn" role="status">
            <p>
              {uncovered.length === 1 ? "One camera is" : `${uncovered.length} cameras are`} looking for watched faces, but no rule will alert anyone about {uncovered.length === 1 ? "it" : "them"}:
              matches there are only recorded. Create the rule so a watched face raises an alarm.
            </p>
            <button type="button" className="button" data-variant="primary" disabled={createRule.isPending} onClick={() => makeRule(uncovered)}>Create an alert rule for these cameras</button>
          </div>
        ) : null}
      </section>
      <section className="field-group settings-block" aria-label="Watch list">
        <h2 className="field-heading">People on the list</h2>
        <ul className="status-list" aria-label="Watched faces">
          {(watch.data?.entries ?? []).map(entry => (
            <Entry key={entry.id} entry={entry} pending={updateEntry.isPending || removeEntry.isPending}
                   save={body => updateEntry.mutate({ id: entry.id, body })} remove={() => removeEntry.mutate(entry.id)} />
          ))}
          {watch.data?.entries.length === 0 ? <li className="muted">Nobody is on the list. Add a face from a recording in Playback.</li> : null}
        </ul>
      </section>
      <section className="field-group settings-block" aria-label="Matches found">
        <h2 className="field-heading">Matches found</h2>
        {!showSightings ? (
          <button type="button" className="button" onClick={() => setShowSightings(true)}>Show recent matches</button>
        ) : (
          <>
          <div className="field-actions">
            <button type="button" className="button" disabled={sightings.isFetching} onClick={() => void sightings.refetch()}>Refresh matches</button>
          </div>
          <ul className="status-list" aria-label="Recent matches">
            {(sightings.data?.sightings ?? []).map(item => (
              <li key={item.id}>
                {item.has_picture ? <img src={`/api/faces/sightings/${item.id}/picture`} alt="" width={48} height={48} className="face-thumb" /> : null}
                <strong>{item.entry_name}</strong>
                <span className="muted">{item.camera_name} · {new Date(item.seen_at).toLocaleString()} · {Math.round(item.similarity * 100)}% alike</span>
              </li>
            ))}
            {sightings.data?.sightings.length === 0 ? <li className="muted">No matches yet.</li> : null}
          </ul>
          </>
        )}
        <p className="muted field-hint">Looking at matches and their pictures is recorded in the audit log.</p>
      </section>
    </>
  );
}
