import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useDisplaySettings } from "@/api/queries";
import type { Camera } from "@/api/types";
import { Modal } from "@/components/Modal";
import { createPreset, deletePreset, deleteTour, getPtz, goToPreset, ptzKey, renamePreset, saveTour, type PtzPreset, type PtzTour, type TourDefinition } from "@/features/ptz/api";
import { PtzPad } from "@/features/ptz/PtzPad";
import { failureText } from "../tabDraft";
import type { ScheduleInterval } from "./api";
import { ScheduleEditor, scheduleProblem } from "./ScheduleEditor";

const STATE_LABEL = { idle: "Waiting for its schedule", running: "Patrolling", paused: "Paused while someone controls the camera" } as const;

function PresetRow({ camera, preset, admin, onChanged, onError }: { camera: Camera; preset: PtzPreset; admin: boolean; onChanged: () => void; onError: (text: string) => void }) {
  const [name, setName] = useState(preset.name);
  return (
    <li className="ptz-preset-row">
      {admin ? <input aria-label={`Name of preset ${preset.name}`} value={name} maxLength={60} onChange={event => setName(event.target.value)} /> : <span>{preset.name}</span>}
      <span className="field-actions">
        <button type="button" className="button" onClick={() => void goToPreset(camera.id, preset.id).catch(error => onError(failureText(error)))}>Go</button>
        {admin ? (
          <>
            <button type="button" className="button" disabled={!name.trim() || name.trim() === preset.name}
                    onClick={() => void renamePreset(camera.id, preset.id, name).then(onChanged).catch(error => onError(failureText(error)))}>Rename</button>
            <button type="button" className="button" data-variant="danger" aria-label={`Delete preset ${preset.name}`}
                    onClick={() => void deletePreset(camera.id, preset.id).then(onChanged).catch(error => onError(failureText(error)))}>Delete</button>
          </>
        ) : null}
      </span>
    </li>
  );
}

function TourDialog({ camera, tour, presets, onClose, onSaved }: { camera: Camera; tour: PtzTour | null; presets: PtzPreset[]; onClose: () => void; onSaved: () => void }) {
  const display = useDisplaySettings();
  const [draft, setDraft] = useState<TourDefinition>(() => tour
    ? { name: tour.name, enabled: tour.enabled, steps: tour.steps, schedule: tour.schedule }
    : { name: "", enabled: true, steps: presets.slice(0, 2).map(preset => ({ preset_id: preset.id, dwell_seconds: 30 })), schedule: null });
  const [error, setError] = useState<string | null>(null);
  const intervals: ScheduleInterval[] = (draft.schedule?.intervals ?? []).map(interval => ({ ...interval, mode: "continuous" }));
  const problem = !draft.name.trim() ? "The tour needs a name."
    : !draft.steps.length ? "Add at least one step."
      : draft.steps.some(step => !(step.dwell_seconds >= 3)) ? "Stay at least 3 seconds on each preset."
        : draft.schedule && !draft.schedule.intervals.length ? "Add a schedule interval, or run the tour at all times."
          : draft.schedule ? scheduleProblem(intervals) : null;
  const step = (index: number, change: Partial<TourDefinition["steps"][number]>) =>
    setDraft({ ...draft, steps: draft.steps.map((item, at) => (at === index ? { ...item, ...change } : item)) });

  return (
    <Modal open onOpenChange={open => !open && onClose()} title={tour ? `Edit ${tour.name}` : "New tour"} wide
           description="A tour visits presets in order, staying the given time on each, and pauses while someone controls the camera."
           footer={<>
             <button type="button" className="button" onClick={onClose}>Cancel</button>
             <button type="button" className="button" data-variant="primary" disabled={Boolean(problem)}
                     onClick={() => void saveTour(camera.id, draft, tour?.id).then(onSaved).catch(failure => setError(failureText(failure)))}>Save tour</button>
           </>}>
      <form className="tab-form" aria-label="Tour" onSubmit={event => event.preventDefault()}>
        <label className="field"><span>Name</span><input value={draft.name} maxLength={60} onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} /> Enabled</label>
        <fieldset className="field-group">
          <legend>Steps</legend>
          <ol className="choice-list">
            {draft.steps.map((item, index) => (
              <li key={index} className="ptz-step">
                <select aria-label={`Step ${index + 1} preset`} value={item.preset_id} onChange={event => step(index, { preset_id: event.target.value })}>
                  {presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
                </select>
                <label className="field"><span>Stay (seconds)</span>
                  <input type="number" min={3} max={3600} value={Number.isNaN(item.dwell_seconds) ? "" : item.dwell_seconds}
                         onChange={event => step(index, { dwell_seconds: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
                </label>
                <button type="button" className="button" aria-label={`Remove step ${index + 1}`} onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, at) => at !== index) })}>Remove</button>
              </li>
            ))}
          </ol>
          <div className="field-actions">
            <button type="button" className="button" disabled={!presets.length || draft.steps.length >= 32}
                    onClick={() => setDraft({ ...draft, steps: [...draft.steps, { preset_id: presets[0]!.id, dwell_seconds: 30 }] })}>Add step</button>
          </div>
        </fieldset>
        <label className="check"><input type="checkbox" checked={draft.schedule !== null}
                                        onChange={event => setDraft({ ...draft, schedule: event.target.checked ? { timezone: display.data?.time_zone ?? "UTC", intervals: [] } : null })} /> Only at scheduled times</label>
        {draft.schedule ? (
          <>
            <label className="field"><span>Time zone</span><input value={draft.schedule.timezone} onChange={event => setDraft({ ...draft, schedule: { ...draft.schedule!, timezone: event.target.value } })} /></label>
            <ScheduleEditor intervals={intervals} withModes={false}
                            onChange={next => setDraft({ ...draft, schedule: { ...draft.schedule!, intervals: next.map(({ days, start, end }) => ({ days, start, end })) } })} />
          </>
        ) : null}
        {problem ? <p className="muted field-hint">{problem}</p> : null}
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      </form>
    </Modal>
  );
}

/** Move the camera, save and manage its presets, and set up patrol tours. Operators may move and recall presets. */
export function PtzTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const state = useQuery({ queryKey: ptzKey(camera.id), queryFn: ({ signal }) => getPtz(camera.id, signal) });
  const [name, setName] = useState("");
  const [message, setMessage] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const [editing, setEditing] = useState<PtzTour | "new" | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: ptzKey(camera.id) });
  const failed = (text: string) => setMessage({ tone: "bad", text });

  if (state.isPending) return <p className="muted">Asking the camera what it can do…</p>;
  if (state.error) return <p className="notice" data-tone="bad" role="alert">{failureText(state.error, "PTZ could not be loaded.")}</p>;
  const data = state.data!;
  if (!data.supported) return <p className="notice">{data.reason ?? "This camera cannot be moved."}</p>;
  const presetName = (id: string) => data.presets.find(preset => preset.id === id)?.name ?? "a removed preset";

  return (
    <div className="tab-form ptz-tab">
      <section className="field-group" aria-label="Move the camera">
        <h3 className="field-heading">Move</h3>
        <p className="muted field-hint">Press and hold to move. One person controls a camera at a time; control passes on {data.lease_seconds} seconds after the last move.</p>
        <PtzPad cameraId={camera.id} />
      </section>

      <section className="field-group" aria-label="Presets">
        <h3 className="field-heading">Presets</h3>
        {data.presets.length ? (
          <ul className="choice-list">
            {data.presets.map(preset => <PresetRow key={preset.id + preset.name} camera={camera} preset={preset} admin={admin} onChanged={refresh} onError={failed} />)}
          </ul>
        ) : <p className="muted field-hint">No presets yet.</p>}
        {admin ? (
          <form className="field-actions" aria-label="New preset" onSubmit={event => {
            event.preventDefault();
            void createPreset(camera.id, name).then(preset => { setName(""); setMessage({ tone: "good", text: `Saved ${preset.name}.` }); refresh(); }).catch(error => failed(failureText(error)));
          }}>
            <label className="field"><span>Save the current position as</span><input value={name} maxLength={60} onChange={event => setName(event.target.value)} /></label>
            <button type="submit" className="button" disabled={!name.trim()}>Save preset</button>
          </form>
        ) : null}
      </section>

      <section className="field-group" aria-label="Tours">
        <h3 className="field-heading">Tours</h3>
        {data.tours.length ? (
          <ul className="choice-list">
            {data.tours.map(tour => (
              <li key={tour.id} className="ptz-preset-row">
                <span><strong>{tour.name}</strong> · {tour.enabled ? STATE_LABEL[tour.state] : "Disabled"}
                  <span className="muted"> · {tour.steps.map(step => `${presetName(step.preset_id)} ${step.dwell_seconds}s`).join(" → ")}</span></span>
                {admin ? (
                  <span className="field-actions">
                    <button type="button" className="button" onClick={() => setEditing(tour)}>Edit</button>
                    <button type="button" className="button" data-variant="danger" aria-label={`Delete tour ${tour.name}`}
                            onClick={() => void deleteTour(camera.id, tour.id).then(refresh).catch(error => failed(failureText(error)))}>Delete</button>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <p className="muted field-hint">No tours. A tour patrols presets on a schedule.</p>}
        {admin ? <button type="button" className="button" disabled={!data.presets.length} onClick={() => setEditing("new")}>New tour</button> : null}
      </section>
      {message ? <p className="notice" data-tone={message.tone} role={message.tone === "good" ? "status" : "alert"}>{message.text}</p> : null}
      {editing ? (
        <TourDialog camera={camera} tour={editing === "new" ? null : editing} presets={data.presets} onClose={() => setEditing(null)}
                    onSaved={() => { setEditing(null); setMessage({ tone: "good", text: "Tour saved." }); refresh(); }} />
      ) : null}
    </div>
  );
}
