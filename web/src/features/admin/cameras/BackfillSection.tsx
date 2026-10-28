import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { BackfillJob } from "@/api/types";
import { failureText } from "../tabDraft";
import { checkBackfill, getBackfill, saveBackfill } from "./api";

const JOB_WORD: Record<BackfillJob["state"], string> = {
  pending: "Waiting",
  running: "Running",
  done: "Filled",
  partial: "Partly filled",
  failed: "Failed",
  unsupported: "Not supported",
};

const shortTime = (iso: string) => new Date(iso).toLocaleString();

/**
 * Backfill from the camera's own recording (ONVIF Profile G): switched on per camera, with a bandwidth limit.
 * It saves on its own (not with the rest of the recording settings) because it has its own request.
 */
export function BackfillSection({ cameraId }: { cameraId: string }) {
  const client = useQueryClient();
  const status = useQuery({ queryKey: ["cameras", cameraId, "backfill"], queryFn: ({ signal }) => getBackfill(cameraId, signal), refetchInterval: 15_000 });
  const [enabled, setEnabled] = useState(false);
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const data = status.data;
  useEffect(() => {
    if (data) {
      setEnabled(data.enabled);
      setLimit(data.limit_kbps === null ? "" : String(data.limit_kbps));
    }
  }, [data?.enabled, data?.limit_kbps]); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(action: () => Promise<unknown>, done: string) {
    setBusy(true);
    setOutcome(null);
    try {
      await action();
      setOutcome({ tone: "good", text: done });
      await client.invalidateQueries({ queryKey: ["cameras", cameraId, "backfill"] });
    } catch (error) {
      setOutcome({ tone: "bad", text: failureText(error, "That could not be done.") });
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <p className="muted" role="status">{status.isError ? "Backfill settings could not be loaded." : "Loading backfill settings…"}</p>;
  const dirty = enabled !== data.enabled || limit !== (data.limit_kbps === null ? "" : String(data.limit_kbps));
  return (
    <fieldset className="field-group" aria-label="Backfill from the camera">
      <legend>Backfill from the camera's own recording</legend>
      <p className="field-hint">After an outage, fetch what the camera recorded on its own card (ONVIF Profile G) and add it to the timeline, marked as backfilled.</p>
      <label className="check"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Backfill this camera</label>
      <label className="field"><span>Bandwidth limit (kbit/s)</span>
        <input type="number" min={64} max={1000000} placeholder={`${data.default_limit_kbps} (default)`} value={limit} onChange={event => setLimit(event.target.value)} />
      </label>
      <p className="field-hint" role="status" data-tone={data.support.state === "unsupported" ? "warn" : undefined}>
        {data.support.state === "unknown" ? "Not checked yet: it is checked when the first outage is filled." : data.support.message}
      </p>
      <div className="field-actions">
        <button type="button" className="button" data-variant="primary" disabled={busy || !dirty}
                onClick={() => void run(() => saveBackfill(cameraId, { enabled, limit_kbps: limit.trim() ? Number(limit) : null }), "Backfill settings saved.")}>Save backfill</button>
        <button type="button" className="button" disabled={busy} onClick={() => void run(() => checkBackfill(cameraId), "Checked the camera.")}>Check the camera</button>
        {outcome ? <span role={outcome.tone === "bad" ? "alert" : "status"} className={outcome.tone === "bad" ? "field-error" : "test-ok"}>{outcome.text}</span> : null}
      </div>
      {data.jobs.length ? (
        <ul className="backfill-jobs" aria-label="Recent backfills">
          {data.jobs.slice(0, 5).map(job => (
            <li key={job.id}>
              <span><strong>{JOB_WORD[job.state]}</strong> · {shortTime(job.starts_at)} – {shortTime(job.ends_at)}{job.filled_seconds ? ` · ${Math.round(job.filled_seconds)} s fetched` : ""}</span>
              {job.message ? <span className="muted">{job.message}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </fieldset>
  );
}
