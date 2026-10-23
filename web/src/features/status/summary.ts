import type { Camera, ExportJob, Incident } from "@/api/types";

export type Tone = "good" | "warn" | "bad" | "neutral";

export interface StatusSummary {
  video: { tone: Tone; label: string; cameras: Camera[] };
  recording: { tone: Tone; label: string; failing: Camera[]; uncertain: Camera[] };
  incidents: { tone: Tone; label: string; open: Incident[] };
  jobs: { tone: Tone; label: string; active: ExportJob[]; failed: ExportJob[] };
}

const FAILING = new Set(["camera_unreachable", "write_stalled"]);
const UNCERTAIN = new Set(["uncertain", "motion_source_degraded"]);

/**
 * Live video freshness and recording-write health are reported separately:
 * a camera can show live video while its footage is not being written.
 */
export function summarize(cameras: Camera[] | undefined, incidents: Incident[] | undefined, jobs: ExportJob[] | undefined): StatusSummary {
  const active = (cameras ?? []).filter(camera => !camera.archived_at && camera.enabled);
  // Ingest freshness is only measured for cameras the recorder is reading.
  const monitored = active.filter(camera => camera.recording_enabled);
  const stale = monitored.filter(camera => !camera.recording_health.video_fresh);
  const failing = active.filter(camera => FAILING.has(camera.recording_health.state));
  const uncertain = active.filter(camera => UNCERTAIN.has(camera.recording_health.state));
  const open = (incidents ?? []).filter(incident => incident.fault_active);
  const running = (jobs ?? []).filter(job => job.status === "queued" || job.status === "running");
  const dayAgo = Date.now() - 24 * 3600_000;
  const failed = (jobs ?? []).filter(job => job.status === "failed" && Date.parse(job.created_at) > dayAgo);
  return {
    video: cameras === undefined
      ? { tone: "neutral", label: "Video: checking", cameras: [] }
      : !monitored.length
        ? { tone: "neutral", label: "Video: not monitored", cameras: [] }
        : stale.length
          ? { tone: "warn", label: `Video: ${stale.length} stale`, cameras: stale }
          : { tone: "good", label: `Video: ${monitored.length} fresh`, cameras: [] },
    recording: cameras === undefined
      ? { tone: "neutral", label: "Recording: checking", failing: [], uncertain: [] }
      : failing.length
        ? { tone: "bad", label: `Recording: ${failing.length} failing`, failing, uncertain }
        : uncertain.length
          ? { tone: "warn", label: `Recording: ${uncertain.length} uncertain`, failing, uncertain }
          : { tone: "good", label: "Recording: healthy", failing, uncertain },
    incidents: incidents === undefined
      ? { tone: "neutral", label: "Incidents: checking", open: [] }
      : open.length
        ? { tone: open.some(incident => incident.state === "active") ? "bad" : "warn", label: `Incidents: ${open.length}`, open }
        : { tone: "good", label: "No incidents", open },
    jobs: running.length
      ? { tone: "neutral", label: `Exports: ${running.length} running`, active: running, failed }
      : failed.length
        ? { tone: "warn", label: `Exports: ${failed.length} failed`, active: running, failed }
        : { tone: "neutral", label: "No exports running", active: running, failed },
  };
}
