import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
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

interface Verification {
  valid: boolean;
  chain_error: string | null;
  signed_upto: number;
  segments: { segment_id: number; starts_at: string; ends_at: string; status: string }[];
  problems: { segment_id: number; starts_at: string; ends_at: string; status: string }[];
}

const STATUS_LABEL: Record<string, string> = {
  ok: "Verified",
  ok_unsigned_yet: "Matches the chain; signed within a minute",
  not_signed: "Not signed (recorded before signing began)",
  pending: "Not hashed yet",
  out_of_order: "Out of order: its time range differs from the chain",
  unavailable: "Marked unavailable",
  missing: "Missing: removed without a recorded deletion",
  altered: "Altered: the file no longer matches its hash",
};

const RANGES = [{ label: "Last hour", hours: 1 }, { label: "Last 24 hours", hours: 24 }, { label: "Last 7 days", hours: 24 * 7 }, { label: "Last 31 days", hours: 24 * 31 }];

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
      <FootageVerification camera={camera} />
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

/** Checks the camera's footage against its signed hash chain and names any segment that does not match. */
function FootageVerification({ camera }: { camera: Camera }) {
  const [hours, setHours] = useState(24);
  const verify = useMutation({
    mutationFn: () => {
      const end = new Date();
      const params = new URLSearchParams({ start: new Date(end.getTime() - hours * 3_600_000).toISOString(), end: end.toISOString() });
      return api<Verification>(`/api/cameras/${encodeURIComponent(camera.id)}/footage/verify?${params}`);
    },
  });
  const result = verify.data;
  const counts = new Map<string, number>();
  for (const segment of result?.segments ?? []) counts.set(segment.status, (counts.get(segment.status) ?? 0) + 1);

  return (
    <section className="field-group" aria-label="Footage verification">
      <h3 className="field-heading">Verify footage</h3>
      <p className="muted field-hint">Every recorded segment is hashed into a signed chain. Verifying checks the files against it, so altered, missing or reordered footage is named. Deletions by retention or an administrator are expected.</p>
      <div className="field-actions">
        <label className="field"><span>Range</span>
          <select value={hours} onChange={event => { setHours(Number(event.target.value)); verify.reset(); }}>
            {RANGES.map(range => <option key={range.hours} value={range.hours}>{range.label}</option>)}
          </select>
        </label>
        <button type="button" className="button" disabled={verify.isPending} onClick={() => verify.mutate()}>{verify.isPending ? "Verifying…" : "Verify footage"}</button>
      </div>
      {verify.error ? <p className="notice" data-tone="bad" role="alert">Footage could not be verified: {verify.error.message}.</p> : null}
      {result ? (
        <div role="status">
          <p className="notice" data-tone={result.valid ? "good" : "bad"}>
            {result.chain_error ? `The chain cannot be trusted: ${result.chain_error}.`
              : result.valid ? `${result.segments.length} segment${result.segments.length === 1 ? "" : "s"} checked; no tampering found.`
                : `${result.problems.length} segment${result.problems.length === 1 ? "" : "s"} failed verification.`}
          </p>
          {counts.size ? <p className="muted">{[...counts].map(([status, count]) => `${STATUS_LABEL[status] ?? status}: ${count}`).join(" · ")}</p> : null}
          {result.problems.length ? (
            <ul className="plain-list" aria-label="Segments that failed verification">
              {result.problems.map(problem => (
                <li key={problem.segment_id}>
                  <strong>{STATUS_LABEL[problem.status] ?? problem.status}</strong> · segment {problem.segment_id} · {new Date(problem.starts_at).toLocaleString()} to {new Date(problem.ends_at).toLocaleTimeString()}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
