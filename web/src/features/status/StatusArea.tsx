import { useState } from "react";
import { ApiError } from "@/api/client";
import { useAcknowledgeIncident, useCameras, useExportJobs, useOpenIncidents } from "@/api/queries";
import type { Camera } from "@/api/types";
import { Drawer } from "@/components/Drawer";
import { formatRelative } from "@/lib/time";
import { summarize, type StatusSummary, type Tone } from "./summary";

type Section = "video" | "recording" | "incidents" | "jobs";

function Chip({ tone, label, onClick }: { tone: Tone; label: string; onClick: () => void }) {
  return (
    <button type="button" className="status-chip" onClick={onClick}>
      <span className="status-dot" data-tone={tone} aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}

/** Compact operational status that stays available in every workspace. */
export function StatusArea() {
  const cameras = useCameras();
  const incidents = useOpenIncidents();
  const jobs = useExportJobs();
  const [section, setSection] = useState<Section | null>(null);
  const summary = summarize(cameras.data, incidents.data, jobs.data);
  const unreachable = cameras.isError || incidents.isError;
  return (
    <>
      <div className="status-area" role="status" aria-label="Operational status">
        {unreachable ? <Chip tone="bad" label="Status unavailable" onClick={() => setSection("incidents")} /> : null}
        <Chip tone={summary.recording.tone} label={summary.recording.label} onClick={() => setSection("recording")} />
        <Chip tone={summary.video.tone} label={summary.video.label} onClick={() => setSection("video")} />
        <Chip tone={summary.incidents.tone} label={summary.incidents.label} onClick={() => setSection("incidents")} />
        <Chip tone={summary.jobs.tone} label={summary.jobs.label} onClick={() => setSection("jobs")} />
      </div>
      <Drawer open={section !== null} onOpenChange={open => !open && setSection(null)} title="Operational status"
              description="Live video health and recording health are reported separately; a live picture does not prove footage is being written.">
        <StatusDetails summary={summary} focus={section} />
      </Drawer>
    </>
  );
}

function CameraList({ cameras, empty }: { cameras: Camera[]; empty: string }) {
  if (!cameras.length) return <p className="muted">{empty}</p>;
  return (
    <ul className="status-list">
      {cameras.map(camera => (
        <li key={camera.id}>
          <strong>{camera.name}</strong>
          <span className="muted">{camera.recording_health.detail}</span>
        </li>
      ))}
    </ul>
  );
}

function StatusDetails({ summary, focus }: { summary: StatusSummary; focus: Section | null }) {
  const acknowledge = useAcknowledgeIncident();
  return (
    <div className="status-details">
      <section aria-labelledby="status-recording" data-focused={focus === "recording"}>
        <h3 id="status-recording">Recording</h3>
        <CameraList cameras={[...summary.recording.failing, ...summary.recording.uncertain]} empty="Every enabled camera is recording as its policy expects." />
      </section>
      <section aria-labelledby="status-video" data-focused={focus === "video"}>
        <h3 id="status-video">Live video</h3>
        <CameraList cameras={summary.video.cameras} empty="Every recorded camera has delivered fresh video recently. Freshness is measured for cameras with recording enabled." />
      </section>
      <section aria-labelledby="status-incidents" data-focused={focus === "incidents"}>
        <h3 id="status-incidents">Incidents</h3>
        {acknowledge.error ? <p className="notice" data-tone="bad" role="alert">{acknowledge.error instanceof ApiError ? acknowledge.error.detail : "Could not acknowledge the incident."}</p> : null}
        {summary.incidents.open.length ? (
          <ul className="status-list">
            {summary.incidents.open.map(incident => (
              <li key={incident.id}>
                <strong>{incident.summary}</strong>
                <span className="muted">
                  {incident.state === "acknowledged" ? "Acknowledged, still active" : "Active"} · last seen {formatRelative(incident.last_seen_at)}
                  {incident.recurrence_count > 1 ? ` · occurred ${incident.recurrence_count} times` : ""}
                </span>
                {incident.state === "active" ? (
                  <button type="button" className="button" onClick={() => acknowledge.mutate(incident.id)} disabled={acknowledge.isPending}>
                    Acknowledge
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No active incidents.</p>
        )}
      </section>
      <section aria-labelledby="status-jobs" data-focused={focus === "jobs"}>
        <h3 id="status-jobs">Export jobs</h3>
        {summary.jobs.active.length || summary.jobs.failed.length ? (
          <ul className="status-list">
            {[...summary.jobs.active, ...summary.jobs.failed].map(job => (
              <li key={job.id}>
                <strong>{job.status === "failed" ? "Failed export" : job.status === "queued" ? "Queued export" : `Exporting · ${job.progress}%`}</strong>
                <span className="muted">{job.error ?? `Started ${formatRelative(job.created_at)}`}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No exports are running.</p>
        )}
      </section>
    </div>
  );
}
