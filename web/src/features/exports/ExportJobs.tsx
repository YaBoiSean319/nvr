import { ApiError } from "@/api/client";
import { useCameras, useCancelExport, useDisplaySettings, useExportJobs, useRetryExport } from "@/api/queries";
import type { ExportJob } from "@/api/types";
import { formatRelative } from "@/lib/time";
import { formatSiteDateTime } from "@/lib/zonedTime";

const SHOWN = 20;

function describe(job: ExportJob): string {
  switch (job.status) {
    case "queued": return job.cancel_requested ? "Cancelling…" : "Queued";
    case "running": return job.cancel_requested ? "Cancelling…" : `Running · ${job.progress}%`;
    case "completed": return job.download_url ? "Ready" : "Expired";
    case "failed": return "Failed";
    case "cancelled": return "Cancelled";
  }
}

/**
 * Every export job of this person (all jobs for administrators), newest first. Jobs live on
 * the server, so this list survives navigation, reloads and other sessions.
 */
export function ExportJobs() {
  const jobs = useExportJobs();
  const cameras = useCameras();
  const display = useDisplaySettings();
  const cancel = useCancelExport();
  const retry = useRetryExport();
  const names = new Map((cameras.data ?? []).map(camera => [camera.id, camera.name]));
  const failure = cancel.error ?? retry.error;
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data, false) : iso);
  const list = [...(jobs.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, SHOWN);

  if (jobs.isPending) return <p className="muted">Loading export jobs…</p>;
  if (!list.length) return <p className="muted">No exports yet. Mark a range in Playback to export footage.</p>;
  return (
    <>
      {failure ? <p className="notice" data-tone="bad" role="alert">{failure instanceof ApiError ? failure.detail : "The export could not be changed."}</p> : null}
      <ul className="status-list export-jobs" aria-label="Export jobs">
        {list.map(job => {
          const cameraNames = job.camera_ids.map(id => names.get(id) ?? "Removed camera");
          const active = job.status === "queued" || job.status === "running";
          return (
            <li key={job.id} className="export-job" data-status={job.status} aria-label={`Export ${when(job.start)}`}>
              <strong>{describe(job)}</strong>
              <span>{when(job.start)} – {when(job.end)}</span>
              <span className="muted">
                {cameraNames.length > 3 ? `${cameraNames.slice(0, 3).join(", ")} and ${cameraNames.length - 3} more` : cameraNames.join(", ")}
                {" · "}{job.output_modes.map(mode => (mode === "grid" ? `grid ${job.grid_resolution === "4k" ? "4K" : "1080p"}` : "individual")).join(" + ")}
                {job.include_audio ? " · audio" : ""}
              </span>
              {job.status === "running" ? <progress max={100} value={job.progress} aria-label="Export progress" /> : null}
              {job.error ? <span className="export-error">{job.error}</span> : null}
              {job.retry_of ? <span className="muted">Retry of an earlier attempt</span> : null}
              {job.status === "completed" && job.expires_at ? (
                <span className="muted">{job.download_url ? `Available until ${when(job.expires_at)}` : `Expired ${formatRelative(job.expires_at)}`}</span>
              ) : null}
              <span className="export-actions">
                {job.download_url ? <a className="button" data-variant="primary" href={job.download_url} download>Download</a> : null}
                {active && !job.cancel_requested ? (
                  <button type="button" className="button" disabled={cancel.isPending} onClick={() => cancel.mutate(job.id)}>Cancel</button>
                ) : null}
                {job.status === "failed" || job.status === "cancelled" ? (
                  <button type="button" className="button" disabled={retry.isPending} onClick={() => retry.mutate(job.id)}>Retry</button>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );
}
