import { useState } from "react";
import { ApiError } from "@/api/client";
import { useChangeRedactionReview, useRedactionReview } from "@/api/queries";
import type { Camera, RedactionEdit, RedactionRegion, RedactionTrack } from "@/api/types";

interface Props {
  reviewId: string;
  cameras: Map<string, Camera>;
}

function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

const kindName = (track: RedactionTrack) => (track.kind === "face" ? "Face" : "Plate");

/** Region fields as percentages of the picture, for a person typing them in. */
interface Draft { camera: string; from: string; to: string; follow: string; x: string; y: string; width: string; height: string; toX: string; toY: string }

const percent = (text: string) => Number(text) / 100;

/**
 * The review step before a redacted export: every face and plate the models found, a picture to check them against, a
 * way to stop one being blurred, manual regions, and the confirmation an export needs.
 */
export function RedactionPanel({ reviewId, cameras }: Props) {
  const review = useRedactionReview(reviewId);
  const { edit, confirm } = useChangeRedactionReview(reviewId);
  const data = review.data;
  const [look, setLook] = useState<{ camera: string; at: number } | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const failure = edit.error ?? confirm.error;
  const name = (id: string) => cameras.get(id)?.name ?? "Camera";

  if (!data) return <p className="muted" role="status">Starting the review…</p>;
  if (data.state === "queued" || data.state === "analysing") {
    return (
      <div className="redaction-panel" role="status">
        <p>Looking for faces and plates… {data.progress}%</p>
        <progress max={100} value={data.progress} aria-label="Review progress" />
      </div>
    );
  }
  if (data.state === "failed") return <p className="notice" data-tone="bad" role="alert">The review failed: {data.error ?? "unknown reason"}</p>;

  const locked = data.state !== "ready";
  const change = (next: RedactionEdit) => edit.mutate(next);
  const kept = data.tracks.filter(track => !track.blur).map(track => track.id);
  const manual = data.manual.map(region => ({ camera_id: region.camera_id, start: region.start, end: region.end, follow_track_id: region.follow_track_id, scale: region.scale, keyframes: region.keyframes }));
  const shown = look ?? (data.tracks[0] ? { camera: data.tracks[0].camera_id, at: (data.tracks[0].start + data.tracks[0].end) / 2 } : null);
  const duration = shown ? data.durations[shown.camera] ?? 0 : 0;

  function addRegion() {
    if (!draft) return;
    const from = Number(draft.from), to = Number(draft.to);
    const size = { width: percent(draft.width), height: percent(draft.height) };
    const region: Omit<RedactionRegion, "id"> = draft.follow
      ? { camera_id: draft.camera, start: from, end: to, follow_track_id: draft.follow, scale: 1, keyframes: [] }
      : { camera_id: draft.camera, start: from, end: to, follow_track_id: null, scale: 1, keyframes: [
          { t: from, x: percent(draft.x), y: percent(draft.y), ...size },
          ...(draft.toX && draft.toY ? [{ t: to, x: percent(draft.toX), y: percent(draft.toY), ...size }] : []),
        ] };
    edit.mutate({ manual: [...manual, region] }, { onSuccess: () => setDraft(null) });
  }

  const field = (key: keyof Draft, label: string) => (
    <label className="redaction-field">
      <span>{label}</span>
      <input type="number" value={draft?.[key] ?? ""} onChange={event => draft && setDraft({ ...draft, [key]: event.target.value })} inputMode="decimal" />
    </label>
  );

  return (
    <div className="redaction-panel">
      <p className="muted">
        {data.tracks.length
          ? `${data.tracks.length} found. Everything marked Blur is blurred in the export; untick Blur for a subject that must stay visible.`
          : "Nothing was found. You can still add regions to blur."}
      </p>
      {data.state === "confirmed" ? <p className="notice" data-tone="good" role="status">Confirmed. The export will use these regions.</p> : null}
      {data.camera_ids.map(cameraId => {
        const tracks = data.tracks.filter(track => track.camera_id === cameraId);
        if (!tracks.length) return null;
        return (
          <fieldset key={cameraId}>
            <legend>{name(cameraId)}</legend>
            <ul className="redaction-tracks" aria-label={`Faces and plates on ${name(cameraId)}`}>
              {tracks.map(track => (
                <li key={track.id}>
                  <label>
                    <input type="checkbox" checked={track.blur} disabled={locked || edit.isPending}
                      onChange={event => change({ keep: event.target.checked ? kept.filter(id => id !== track.id) : [...kept, track.id] })} />
                    <span>Blur {kindName(track).toLowerCase()}</span>
                  </label>
                  <span className="muted">{clock(track.start)}–{clock(track.end)}</span>
                  <button type="button" className="button" onClick={() => setLook({ camera: cameraId, at: (track.start + track.end) / 2 })}>Show</button>
                </li>
              ))}
            </ul>
          </fieldset>
        );
      })}

      {shown ? (
        <figure className="redaction-preview">
          <img alt={`${name(shown.camera)} at ${clock(shown.at)}, with blurred areas outlined in red and kept ones in green`}
            src={`/api/redaction/reviews/${encodeURIComponent(reviewId)}/frame?camera_id=${encodeURIComponent(shown.camera)}&t=${shown.at.toFixed(2)}&v=${data.edited_at ?? ""}`} />
          <input type="range" min={0} max={Math.max(0.1, duration - 0.1)} step={0.2} value={Math.min(shown.at, duration)} aria-label="Moment shown"
            onChange={event => setLook({ camera: shown.camera, at: Number(event.target.value) })} />
          <figcaption className="muted">{name(shown.camera)} · {clock(shown.at)}</figcaption>
        </figure>
      ) : null}

      {data.manual.length ? (
        <ul className="redaction-tracks" aria-label="Manual regions">
          {data.manual.map((region, index) => (
            <li key={region.id}>
              <span>{name(region.camera_id)} · {region.follow_track_id ? "follows a track" : "by position"}</span>
              <span className="muted">{clock(region.start)}–{clock(region.end)}</span>
              <button type="button" className="button" disabled={locked || edit.isPending} onClick={() => change({ manual: manual.filter((_, position) => position !== index) })}>Remove</button>
            </li>
          ))}
        </ul>
      ) : null}

      {!locked && !draft ? (
        <button type="button" className="button" onClick={() => setDraft({ camera: shown?.camera ?? data.camera_ids[0] ?? "", from: String(Math.round(shown?.at ?? 0)), to: String(Math.min(Math.round((shown?.at ?? 0) + 5), Math.floor(duration || 5))), follow: "", x: "40", y: "40", width: "10", height: "10", toX: "", toY: "" })}>
          Add a region to blur
        </button>
      ) : null}
      {draft ? (
        <fieldset className="redaction-draft">
          <legend>New region</legend>
          <label className="redaction-field">
            <span>Camera</span>
            <select value={draft.camera} onChange={event => setDraft({ ...draft, camera: event.target.value, follow: "" })}>
              {data.camera_ids.map(id => <option key={id} value={id}>{name(id)}</option>)}
            </select>
          </label>
          {field("from", "From (seconds)")}
          {field("to", "To (seconds)")}
          <label className="redaction-field">
            <span>Follow</span>
            <select value={draft.follow} onChange={event => setDraft({ ...draft, follow: event.target.value })}>
              <option value="">Nothing: place it by position</option>
              {data.tracks.filter(track => track.camera_id === draft.camera).map(track => (
                <option key={track.id} value={track.id}>{kindName(track)} {clock(track.start)}–{clock(track.end)}</option>
              ))}
            </select>
          </label>
          {!draft.follow ? (
            <>
              {field("x", "Left (% of picture)")}
              {field("y", "Top (%)")}
              {field("width", "Width (%)")}
              {field("height", "Height (%)")}
              {field("toX", "Left at the end (%, to move it)")}
              {field("toY", "Top at the end (%)")}
            </>
          ) : null}
          <span className="export-actions">
            <button type="button" className="button" data-variant="primary" disabled={edit.isPending} onClick={addRegion}>Add region</button>
            <button type="button" className="button" onClick={() => setDraft(null)}>Cancel</button>
          </span>
        </fieldset>
      ) : null}

      {failure ? <p className="notice" data-tone="bad" role="alert">{failure instanceof ApiError ? failure.detail : "The review could not be changed."}</p> : null}
      {!locked ? (
        <button type="button" className="button" data-variant="primary" disabled={confirm.isPending || edit.isPending} onClick={() => confirm.mutate()}>
          Confirm these blurs
        </button>
      ) : null}
    </div>
  );
}
