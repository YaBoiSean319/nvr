import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import type { GapReason } from "@/api/types";
import { failureText } from "@/features/admin/tabDraft";
import { REASON_LABEL } from "@/features/playback/gapReasons";
import { formatRelative } from "@/lib/time";
import { formatSiteDateTime } from "@/lib/zonedTime";

interface CheckResult { check: string; kind: "camera" | "volume" | "backup"; name: string; passed: boolean | null; detail: string }
interface SelfTest { latest: { kind: string; finished_at: string; passed: boolean; results: CheckResult[] } | null; schedule: { local_time: string } }
interface Gap { starts_at: string; ends_at: string; reason: string; reason_code: GapReason; seconds: number }
interface CameraCoverage {
  camera_id: string; name: string; policy: { mode: string; source: string; scheduled: boolean };
  expected_seconds: number; missed_seconds: number; coverage_percent: number | null;
  missed_by_reason: Partial<Record<GapReason, number>>; largest_gaps: Gap[];
}
interface Report { start: string; end: string; cameras: CameraCoverage[] }
interface StoredReport { id: number; period_start: string; period_end: string; report: Report }

const LIVE = "last-7-days";

/** Seconds as the largest two units, such as "2 h 5 min". */
function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

/** The nightly self-test, and coverage per camera against its policy with the largest gaps and why they happened. */
export function CoverageSettings() {
  return (
    <>
      <SelfTestSection />
      <CoverageReportSection />
    </>
  );
}

function SelfTestSection() {
  const client = useQueryClient();
  const status = useQuery({ queryKey: ["self-test"], queryFn: ({ signal }) => api<SelfTest>("/api/self-test", { signal }) });
  const run = useMutation({
    mutationFn: () => api<SelfTest>("/api/self-test/run", { method: "POST" }),
    onSuccess: result => {
      client.setQueryData(["self-test"], result);
      void client.invalidateQueries({ queryKey: ["system", "status"] });
    },
  });
  const latest = status.data?.latest;
  return (
    <section className="field-group settings-block" aria-label="Self-test">
      <h2 className="field-heading">Self-test</h2>
      <p className="muted field-hint">
        Every night at {status.data?.schedule.local_time ?? "…"} (site time) the NVR checks that every camera expected to record wrote footage in the last day,
        every recording volume is writable, and the latest configuration backup is fresh. Each failed check raises an incident, which recovers when the check passes again.
      </p>
      {status.isPending ? <p className="muted">Loading…</p> : status.error ? <p className="notice" data-tone="bad" role="alert">{failureText(status.error)}</p> : latest ? (
        <>
          <p className="notice" role="status" data-tone={latest.passed ? "good" : "bad"}>
            {latest.passed ? "Passed" : `Failed ${latest.results.filter(result => result.passed === false).length} check(s)`} · {latest.kind === "manual" ? "run by hand" : "nightly run"} {formatRelative(latest.finished_at)}
          </p>
          <ul className="plain-list" aria-label="Self-test checks">
            {latest.results.map(result => (
              <li key={result.check} data-passed={result.passed === null ? "skipped" : String(result.passed)}>
                <strong>{result.passed === null ? "Not checked" : result.passed ? "Passed" : "Failed"}</strong> · {result.detail}
              </li>
            ))}
          </ul>
        </>
      ) : <p className="muted">The self-test has not run yet.</p>}
      {run.error ? <p className="notice" data-tone="bad" role="alert">The self-test could not run: {failureText(run.error)}.</p> : null}
      <div className="field-actions">
        <button type="button" className="button" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Running…" : "Run self-test now"}</button>
      </div>
    </section>
  );
}

function CoverageReportSection() {
  const display = useDisplaySettings();
  const [choice, setChoice] = useState(LIVE);
  const stored = useQuery({ queryKey: ["coverage", "reports"], queryFn: ({ signal }) => api<{ reports: StoredReport[] }>("/api/coverage/reports", { signal }).then(result => result.reports) });
  const live = useQuery({
    queryKey: ["coverage", "report", LIVE],
    enabled: choice === LIVE,
    queryFn: ({ signal }) => {
      const end = new Date();
      const params = new URLSearchParams({ start: new Date(end.getTime() - 7 * 86_400_000).toISOString(), end: end.toISOString() });
      return api<Report>(`/api/coverage/report?${params}`, { signal });
    },
  });
  const when = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
  const report = choice === LIVE ? live.data : stored.data?.find(item => String(item.id) === choice)?.report;
  const pending = choice === LIVE ? live.isPending : stored.isPending;
  const error = choice === LIVE ? live.error : stored.error;

  return (
    <section className="field-group settings-block" aria-label="Coverage report">
      <h2 className="field-heading">Coverage report</h2>
      <p className="muted field-hint">
        Coverage counts only time each camera's policy expects it to record: time outside its schedule, with recording disabled, or with no motion on a motion camera is left out.
        A report for each week is kept here every Monday; to have it emailed, add a webhook for <code>coverage.weekly_report</code> under Notifications and route it to email.
      </p>
      <label className="field"><span>Report</span>
        <select value={choice} onChange={event => setChoice(event.target.value)}>
          <option value={LIVE}>Last 7 days (now)</option>
          {(stored.data ?? []).map(item => <option key={item.id} value={String(item.id)}>Week of {when(item.period_start)}</option>)}
        </select>
      </label>
      {pending ? <p className="muted">Loading…</p> : error ? <p className="notice" data-tone="bad" role="alert">{failureText(error)}</p> : report ? (
        report.cameras.length ? (
          <div className="inventory-scroll">
            <table className="inventory-table" aria-label="Coverage per camera">
              <thead><tr><th scope="col">Camera</th><th scope="col">Policy</th><th scope="col">Coverage</th><th scope="col">Missed</th><th scope="col">Largest gaps</th></tr></thead>
              <tbody>
                {report.cameras.map(camera => (
                  <tr key={camera.camera_id}>
                    <th scope="row">{camera.name}</th>
                    <td>{camera.policy.scheduled ? "Scheduled" : camera.policy.mode}</td>
                    <td>{camera.coverage_percent === null ? "Not expected to record" : `${camera.coverage_percent}%`}</td>
                    <td>{camera.missed_seconds ? duration(camera.missed_seconds) : "None"}</td>
                    <td>
                      {camera.largest_gaps.length ? (
                        <ul className="plain-list">
                          {camera.largest_gaps.map(gap => (
                            <li key={gap.starts_at}>{duration(gap.seconds)} · {REASON_LABEL[gap.reason_code] ?? gap.reason_code} · {when(gap.starts_at)}</li>
                          ))}
                        </ul>
                      ) : "None"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="muted">No cameras.</p>
      ) : <p className="muted">No report.</p>}
    </section>
  );
}
