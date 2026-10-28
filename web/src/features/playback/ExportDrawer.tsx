import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { useCreateExport, useLenses, useRedactionKinds, useRedactionReview, useStartRedactionReview } from "@/api/queries";
import type { Camera, DisplaySettings, ExportDewarp, TimelineCamera } from "@/api/types";
import { clampView, type Lens } from "@/features/dewarp/geometry";
import { useLiveStore } from "@/features/live/store";
import { useStatusPanel } from "@/features/status/StatusArea";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { formatSiteDateTime, localTimeCandidates, toLocalInput } from "@/lib/zonedTime";
import { playbackClock } from "./clock";
import { intervalsOf } from "./investigationRules";
import { MAX_SEARCH_MS } from "./SearchBar";
import { RedactionPanel } from "./RedactionPanel";
import { usePlaybackStore, type Marks } from "./store";

interface Props {
  settings: DisplaySettings;
  range: { start: number; end: number };
  /** Searched cameras in their Playback order; the grid follows this order. */
  shownIds: string[];
  cameras: Map<string, Camera>;
  timeline: Map<string, TimelineCamera>;
}

function duration(ms: number): string {
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

function covered(row: TimelineCamera | undefined, start: number, end: number): number {
  return (row ? intervalsOf(row) : []).reduce((sum, interval) => sum + Math.max(0, Math.min(end, interval.end) - Math.max(start, interval.start)), 0);
}

/** How a fisheye camera is rendered in the export: the raw circle, a panorama or a virtual view. Starts from how it is shown in Live, so the file matches the screen. */
function DewarpChoice({ camera, lens, value, onChange }: { camera: Camera; lens: Lens; value: ExportDewarp | null; onChange: (value: ExportDewarp | null) => void }) {
  const number = (text: string) => (text === "" ? Number.NaN : Number(text));
  const choose = (choice: string) => {
    if (choice === "raw") return onChange(null);
    const live = useLiveStore.getState().dewarp[camera.id];
    if (choice === "view") {
      const shown = live?.mode === "views" ? live.views[0] : undefined;
      return onChange({ mode: "view", ...(shown ?? clampView(lens.mount, { pan: 0, tilt: lens.mount === "ceiling" ? -45 : lens.mount === "floor" ? 45 : 0, fov: 90 })) });
    }
    onChange({ mode: choice as ExportDewarp["mode"], pan: live && live.mode === choice ? live.pan : 0, tilt: 0, fov: 90 });
  };
  return (
    <div className="export-dewarp">
      <label>
        <span>{camera.name}</span>
        <select aria-label={`How ${camera.name} is rendered`} value={value?.mode ?? "raw"} onChange={event => choose(event.target.value)}>
          <option value="raw">Raw 360° circle</option>
          <option value="panorama">Panorama</option>
          {lens.mount !== "wall" ? <option value="double_panorama">Double panorama</option> : null}
          <option value="view">Virtual view</option>
        </select>
      </label>
      {value?.mode === "view" ? (
        <span className="export-dewarp-view">
          <label><span>Pan (°)</span><input type="number" step={1} value={Number.isNaN(value.pan) ? "" : value.pan} onChange={event => onChange({ ...value, pan: number(event.target.value) })} /></label>
          <label><span>Tilt (°)</span><input type="number" step={1} value={Number.isNaN(value.tilt) ? "" : value.tilt} onChange={event => onChange({ ...value, tilt: number(event.target.value) })} /></label>
          <label><span>Field of view (°)</span><input type="number" step={1} min={20} max={120} value={Number.isNaN(value.fov) ? "" : value.fov} onChange={event => onChange({ ...value, fov: number(event.target.value) })} /></label>
        </span>
      ) : value ? (
        <span className="export-dewarp-view">
          <label><span>Starts at (°)</span><input type="number" step={1} value={Number.isNaN(value.pan) ? "" : value.pan} onChange={event => onChange({ ...value, pan: number(event.target.value) })} /></label>
        </span>
      ) : null}
    </div>
  );
}

/** A precise, second-level editor for one mark, in the site time zone. */
function MarkField({ kind, label, value, fallback, settings, range }: {
  kind: keyof Marks; label: string; value: number | null; fallback: number; settings: DisplaySettings; range: { start: number; end: number };
}) {
  const { setMark } = usePlaybackStore.getState();
  const zone = settings.time_zone;
  const shown = value ?? fallback;
  const [text, setText] = useState(() => toLocalInput(shown, zone, true));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setText(toLocalInput(shown, zone, true));
    setError(null);
  }, [shown, zone]);

  function commit(next: string) {
    setText(next);
    const candidates = localTimeCandidates(next, zone);
    // A repeated wall time resolves to the occurrence inside the search; a skipped one cannot be used.
    const inside = candidates.filter(instant => instant >= range.start && instant <= range.end);
    const chosen = inside.length ? inside.reduce((best, instant) => (Math.abs(instant - shown) < Math.abs(best - shown) ? instant : best)) : candidates[0];
    if (chosen === undefined) {
      setError(next ? `This time does not exist in ${zone}.` : null);
      return;
    }
    setError(null);
    setMark(kind, chosen);
  }

  return (
    <div className="export-mark">
      <label>
        <span>{label}</span>
        <input type="datetime-local" step={1} value={text} onChange={event => commit(event.target.value)} />
      </label>
      <button type="button" className="button" onClick={() => setMark(kind, playbackClock.time())}>Use playhead</button>
      {value === null ? <span className="muted">Search {kind === "in" ? "start" : "end"}</span> : null}
      {error ? <span className="field-error" role="alert">{error}</span> : null}
    </div>
  );
}

/**
 * Export the marked range. It sits beside Playback, so opening or closing it never
 * restarts players or changes the investigation.
 */
export function ExportDrawer({ settings, range, shownIds, cameras, timeline }: Props) {
  const marks = usePlaybackStore(store => store.marks);
  const chosenIds = usePlaybackStore(store => store.exportCameraIds);
  const { setExportOpen, setExportCameraIds } = usePlaybackStore.getState();
  const create = useCreateExport();
  const showJobs = useStatusPanel(state => state.show);
  const [individual, setIndividual] = useState(true);
  const [grid, setGrid] = useState(true);
  const [resolution, setResolution] = useState<"1080p" | "4k">("1080p");
  const [audio, setAudio] = useState(false);
  const [audioSource, setAudioSource] = useState<string>("");
  const [submitted, setSubmitted] = useState(false);
  const kinds = useRedactionKinds();
  const startReview = useStartRedactionReview();
  const [blur, setBlur] = useState<("faces" | "plates")[]>([]);
  // A review belongs to the exact cameras, range and kinds it was made for; changing any of them makes it stale.
  const [review, setReview] = useState<{ id: string; key: string } | null>(null);
  const lenses = useLenses().data ?? {};
  const [dewarp, setDewarp] = useState<Record<string, ExportDewarp>>({});

  const start = marks.in ?? range.start;
  const end = marks.out ?? range.end;
  const selected = useMemo(() => shownIds.filter(id => chosenIds === null || chosenIds.includes(id)), [shownIds, chosenIds]);
  const audioCameras = selected.filter(id => cameras.get(id)?.audio_enabled);
  const coverage = selected.map(id => ({ id, name: cameras.get(id)?.name ?? "Camera", covered: covered(timeline.get(id), start, end) }));
  const span = end - start;
  const blurring = blur.length > 0;
  const fisheyeSelected = selected.filter(id => lenses[id]);
  const dewarped = Object.fromEntries(Object.entries(dewarp).filter(([id]) => selected.includes(id) && lenses[id]));
  const dewarping = Object.keys(dewarped).length > 0;
  const reviewKey = JSON.stringify([selected, start, end, blur]);
  const currentReview = review && review.key === reviewKey ? review.id : null;
  const reviewState = useRedactionReview(currentReview).data?.state;

  const problem = (() => {
    if (end <= start) return "The export end must be after its start.";
    if (start < range.start || end > range.end) return "Export marks must stay within the searched range.";
    if (span > MAX_SEARCH_MS) return "Exports are limited to 24 hours.";
    if (!selected.length) return "Choose at least one camera.";
    if (!individual && !grid) return "Choose at least one output.";
    if (blurring && !individual) return "Blurred exports are individual camera files.";
    if (dewarping && !individual) return "A dewarped view is rendered in the individual camera file: include that output.";
    if (dewarping && blurring) return "Blurring marks regions of the raw picture: choose a blur or a dewarped view, not both.";
    if (Object.values(dewarped).some(view => !(Number.isFinite(view.pan) && Number.isFinite(view.tilt) && view.fov >= 20 && view.fov <= 120))) return "A virtual view needs a pan, a tilt and a field of view of 20 to 120 degrees.";
    if (coverage.every(item => item.covered <= 0)) return "There is no footage in this range for the selected cameras, so there is nothing to export.";
    if (audio && grid && !audioSource) return "Choose which camera's audio the grid uses.";
    return null;
  })();

  function toggleCamera(id: string, on: boolean) {
    const next = on ? [...selected, id] : selected.filter(existing => existing !== id);
    setExportCameraIds(next.length === shownIds.length ? null : next);
  }

  function toggleBlur(kind: "faces" | "plates", on: boolean) {
    const next = on ? [...blur, kind] : blur.filter(item => item !== kind);
    setBlur(next);
    if (next.length) setGrid(false);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (problem) return;
    if (blurring && reviewState !== "confirmed") {
      // The first press looks for faces and plates; the export itself waits for the confirmed review.
      if (!currentReview) startReview.mutate({ camera_ids: selected, start: new Date(start).toISOString(), end: new Date(end).toISOString(), kinds: blur }, { onSuccess: made => setReview({ id: made.id, key: reviewKey }) });
      return;
    }
    // The grid follows the arrangement at the moment of submission.
    const size = [1, 4, 9, 16].find(value => value >= selected.length) ?? MAX_DISPLAYED_CAMERAS;
    const tiles = [...selected, ...Array<null>(size - selected.length).fill(null)];
    setSubmitted(true);
    create.mutate({
      camera_ids: selected,
      start: new Date(start).toISOString(),
      end: new Date(end).toISOString(),
      output_modes: [...(individual ? ["individual" as const] : []), ...(grid ? ["grid" as const] : [])],
      tile_camera_ids: tiles,
      grid_resolution: resolution,
      include_audio: audio && (grid ? Boolean(audioSource) : audioCameras.length > 0),
      audio_camera_id: audio && grid ? audioSource || null : null,
      redaction_review_id: blurring ? currentReview : null,
      ...(dewarping ? { dewarp: dewarped } : {}),
    });
  }

  const missing = coverage.filter(item => item.covered < span);
  return (
    <aside className="export-drawer" aria-label="Export footage">
      <header className="export-head">
        <h2>Export footage</h2>
        <button type="button" className="button icon-button" data-variant="ghost" aria-label="Close export" onClick={() => setExportOpen(false)}>✕</button>
      </header>
      <form className="export-body" onSubmit={submit} noValidate>
        <fieldset>
          <legend>Range</legend>
          <MarkField kind="in" label="From" value={marks.in} fallback={range.start} settings={settings} range={range} />
          <MarkField kind="out" label="To" value={marks.out} fallback={range.end} settings={settings} range={range} />
          <p className="muted export-span">{span > 0 ? `${duration(span)} · ${formatSiteDateTime(start, settings)} – ${formatSiteDateTime(end, settings)}` : ""}</p>
        </fieldset>

        <fieldset>
          <legend>Cameras</legend>
          <ul className="export-cameras">
            {shownIds.map(id => (
              <li key={id}>
                <label>
                  <input type="checkbox" checked={selected.includes(id)} onChange={event => toggleCamera(id, event.target.checked)} />
                  <span>{cameras.get(id)?.name}</span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>

        <fieldset>
          <legend>Outputs</legend>
          <label><input type="checkbox" checked={individual} onChange={event => setIndividual(event.target.checked)} /> Individual camera files</label>
          <label><input type="checkbox" checked={grid} disabled={blurring} onChange={event => setGrid(event.target.checked)} /> Synchronized grid</label>
          {grid ? (
            <div className="export-resolution" role="radiogroup" aria-label="Grid resolution">
              <label><input type="radio" name="resolution" checked={resolution === "1080p"} onChange={() => setResolution("1080p")} /> 1080p</label>
              <label><input type="radio" name="resolution" checked={resolution === "4k"} onChange={() => setResolution("4k")} /> 4K</label>
            </div>
          ) : null}
          {audioCameras.length ? (
            <>
              <label><input type="checkbox" checked={audio} onChange={event => setAudio(event.target.checked)} /> Include audio</label>
              {audio && grid ? (
                <label className="export-audio">
                  <span>Grid audio from</span>
                  <select value={audioSource} onChange={event => setAudioSource(event.target.value)}>
                    <option value="">Choose a camera…</option>
                    {audioCameras.map(id => <option key={id} value={id}>{cameras.get(id)?.name}</option>)}
                  </select>
                </label>
              ) : null}
            </>
          ) : <p className="muted">None of the selected cameras has audio enabled.</p>}
        </fieldset>

        {fisheyeSelected.length ? (
          <fieldset>
            <legend>360° cameras</legend>
            {fisheyeSelected.map(id => (
              <DewarpChoice key={id} camera={cameras.get(id)!} lens={lenses[id]!} value={dewarp[id] ?? null}
                            onChange={next => setDewarp(current => (next ? { ...current, [id]: next } : Object.fromEntries(Object.entries(current).filter(([key]) => key !== id))))} />
            ))}
            <p className="muted">A panorama or view is rendered in the camera's individual file, from the raw recording, with the same geometry as Live. The synchronized grid shows the raw circle.</p>
          </fieldset>
        ) : null}

        {kinds.data?.length ? (
          <fieldset>
            <legend>Blur</legend>
            {kinds.data.map(kind => (
              <label key={kind}>
                <input type="checkbox" checked={blur.includes(kind)} onChange={event => toggleBlur(kind, event.target.checked)} />
                {kind === "faces" ? "Faces" : "Licence plates"}
              </label>
            ))}
            {blurring ? <p className="muted">Blurred exports hold individual camera files only, and you review what is blurred before they are made.</p> : null}
            {blurring && currentReview ? <RedactionPanel reviewId={currentReview} cameras={cameras} /> : null}
            {startReview.error ? <p className="notice" data-tone="bad" role="alert">{startReview.error instanceof ApiError ? startReview.error.detail : "The review could not be started."}</p> : null}
          </fieldset>
        ) : null}

        {missing.length && span > 0 ? (
          <div className="notice export-missing" data-tone="warn" role="status">
            <strong>Known missing footage</strong>
            <ul>
              {missing.map(item => (
                <li key={item.id}>{item.name}: {item.covered <= 0 ? "no footage, left out of the export" : `${duration(span - item.covered)} missing`}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {problem ? <p className="field-error" role="alert">{problem}</p> : null}
        {create.error ? <p className="notice" data-tone="bad" role="alert">{create.error instanceof ApiError ? create.error.detail : "The export could not be started."}</p> : null}
        {submitted && create.isSuccess ? (
          <p className="notice" data-tone="good" role="status">
            Export queued. It continues in the background.
            <button type="button" className="button" onClick={() => showJobs("jobs")}>View export jobs</button>
          </p>
        ) : null}
        <footer className="export-foot">
          <button type="submit" className="button" data-variant="primary" disabled={Boolean(problem) || create.isPending || startReview.isPending || (blurring && Boolean(currentReview) && reviewState !== "confirmed")}>
            {create.isPending ? "Starting…" : blurring && reviewState !== "confirmed" ? (currentReview ? "Confirm the review first" : startReview.isPending ? "Starting…" : "Review blurs") : "Export"}
          </button>
        </footer>
      </form>
    </aside>
  );
}
