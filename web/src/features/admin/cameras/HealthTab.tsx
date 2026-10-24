import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { api } from "@/api/client";
import type { Camera } from "@/api/types";
import { usePlaybackStore } from "@/features/playback/store";
import { formatRelative } from "@/lib/time";
import { getMotionEvents, getRetention } from "./api";
import { CONNECTION_LABEL, HEALTH_LABEL, connectionOf } from "./inventory";

interface Coverage {
  intervals: { coverage_kind: string; reason: string; starts_at: string; ends_at: string | null }[];
}

const KIND_LABEL: Record<string, string> = { covered: "Recorded", expected_idle: "Idle (expected)", failure: "Failure", uncertain: "Uncertain" };

const when = (iso: string | null | undefined) => (iso ? `${formatRelative(iso)} (${new Date(iso).toLocaleString()})` : "Never");

/** Connection, write health, coverage and recent errors, with a way into Playback. */
export function HealthTab({ camera }: { camera: Camera }) {
  const navigate = useNavigate();
  const coverage = useQuery({ queryKey: ["cameras", camera.id, "coverage"], queryFn: ({ signal }) => api<Coverage>(`/api/cameras/${encodeURIComponent(camera.id)}/coverage`, { signal }) });
  const events = useQuery({ queryKey: ["cameras", camera.id, "motion-events"], queryFn: ({ signal }) => getMotionEvents(camera.id, signal) });
  const retention = useQuery({ queryKey: ["cameras", camera.id, "retention"], queryFn: ({ signal }) => getRetention(camera.id, signal) });
  const problems = (coverage.data?.intervals ?? []).filter(interval => interval.coverage_kind === "failure" || interval.coverage_kind === "uncertain").slice(-8).reverse();
  const recentEvents = (events.data?.events ?? []).slice(0, 8);
  const kept = retention.data;

  return (
    <div className="tab-form tab-form-wide">
      <dl className="details">
        <dt>Connection</dt><dd>{CONNECTION_LABEL[connectionOf(camera)]}</dd>
        <dt>Recording health</dt><dd>{HEALTH_LABEL[camera.recording_health.state]}: {camera.recording_health.detail}</dd>
        <dt>Last video received</dt><dd>{when(camera.last_video_at)}</dd>
        <dt>Last footage written</dt><dd>{when(camera.last_footage_write_at)}</dd>
        {camera.recording_fault_since ? <><dt>Failing since</dt><dd>{when(camera.recording_fault_since)}</dd></> : null}
        <dt>Last error</dt><dd>{camera.last_error ?? "None"}</dd>
        <dt>Last stream check</dt><dd>{when(camera.last_probe_at)}</dd>
        {kept ? (
          <>
            <dt>Footage kept</dt>
            <dd>{String(kept.achieved_days ?? 0)} of {kept.retention_days} days{kept.oldest_footage_at ? ` · oldest ${when(kept.oldest_footage_at as string)}` : ""}</dd>
          </>
        ) : null}
      </dl>
      <div className="field-actions">
        <button type="button" className="button" data-variant="primary"
                onClick={() => { usePlaybackStore.getState().openInPlayback(camera.id); navigate("/playback"); }}>Open in Playback</button>
      </div>
      <section className="field-group" aria-label="Coverage problems">
        <h3 className="field-heading">Recent coverage problems</h3>
        {coverage.isPending ? <p className="muted">Loading…</p> : problems.length ? (
          <ul className="plain-list">
            {problems.map(interval => (
              <li key={`${interval.starts_at}-${interval.coverage_kind}`}>
                <strong>{KIND_LABEL[interval.coverage_kind] ?? interval.coverage_kind}</strong> · {interval.reason} · {formatRelative(interval.starts_at)}
                {interval.ends_at ? ` to ${formatRelative(interval.ends_at)}` : " (ongoing)"}
              </li>
            ))}
          </ul>
        ) : <p className="muted">No failures or uncertain coverage recorded.</p>}
      </section>
      <section className="field-group" aria-label="Recent motion events">
        <h3 className="field-heading">Recent motion events</h3>
        {events.isPending ? <p className="muted">Loading…</p> : recentEvents.length ? (
          <ul className="plain-list">
            {recentEvents.map(event => (
              <li key={event.id}>{formatRelative(event.source_event_at)} · {event.status}{event.failure_reason ? ` · ${event.failure_reason}` : ""}</li>
            ))}
          </ul>
        ) : <p className="muted">No motion events recorded.</p>}
      </section>
    </div>
  );
}
