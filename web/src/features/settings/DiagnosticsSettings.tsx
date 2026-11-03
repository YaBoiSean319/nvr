import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";

interface Capture { enabled: boolean; capture_until: string | null; event_count: number; max_events: number; retention_days: number }

interface StartGroup { count: number; median_ms: number | null; p90_ms: number | null; worst_ms: number | null; stages_median_ms?: Record<string, number | null> }
interface LiveStart { camera: string; at: string; profile: string; mode: string; joined_running: boolean; first_media_ms: number; init_ms?: number; spawned_ms?: number; first_keyframe_ms?: number }
interface LiveStartup { summary: { count: number; started: StartGroup; joined: StartGroup }; recent: LiveStart[] }

const seconds = (ms: number | null | undefined) => (ms === null || ms === undefined ? "—" : `${(ms / 1000).toFixed(1)} s`);

/** How long viewers waited for live video since the server started, and where the time went when a camera's stream had to be started. */
function LiveStartupTimes() {
  const report = useQuery({ queryKey: ["live-startup"], queryFn: ({ signal }) => api<LiveStartup>("/api/system/live-startup", { signal }), refetchInterval: 15_000 });
  const data = report.data;
  return (
    <section className="field-group" aria-label="Live video start-up">
      <h2 className="field-heading">Live video start-up</h2>
      <p className="muted field-hint">
        How long people waited from asking for a camera to the first picture leaving the server, since the NVR last started. A viewer who is first on a camera makes the NVR connect to it and wait for its next keyframe;
        later viewers join the running stream. The browser adds its own decoding time on top.
      </p>
      {data && data.summary.count > 0 ? (
        <>
          <table className="data-table" aria-label="Live start-up times">
            <thead><tr><th scope="col">Viewers</th><th scope="col">Count</th><th scope="col">Median</th><th scope="col">Slowest tenth</th><th scope="col">Slowest</th><th scope="col">Connect and first keyframe</th></tr></thead>
            <tbody>
              {([["Started the camera's stream", data.summary.started], ["Joined a running stream", data.summary.joined]] as const).map(([label, group]) => (
                <tr key={label}>
                  <th scope="row">{label}</th><td>{group.count}</td><td>{seconds(group.median_ms)}</td><td>{seconds(group.p90_ms)}</td><td>{seconds(group.worst_ms)}</td>
                  <td>{group.stages_median_ms ? `${seconds(group.stages_median_ms.init_ms)} to start the stream, ${seconds(group.stages_median_ms.first_keyframe_ms)} to its first keyframe` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="data-table" aria-label="Recent live starts">
            <thead><tr><th scope="col">Camera</th><th scope="col">Stream</th><th scope="col">Waited</th><th scope="col">Where the time went</th></tr></thead>
            <tbody>
              {data.recent.slice(0, 15).map((row, index) => (
                <tr key={`${row.at}-${index}`}>
                  <th scope="row">{row.camera || "Camera"}</th>
                  <td>{row.profile === "sub" ? "Sub" : "Main"}{row.mode === "direct" ? "" : ` (${row.mode})`}</td>
                  <td>{seconds(row.first_media_ms)}</td>
                  <td>{row.joined_running ? "Joined a stream that was already running" : `${seconds(row.spawned_ms)} to start the connection, ${seconds(row.init_ms)} until the stream began, ${seconds(row.first_keyframe_ms)} until its first keyframe`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : <p className="muted">{report.error ? failureText(report.error) : "No live video has been opened since the NVR started."}</p>}
    </section>
  );
}

/** A time-limited diagnostic capture and a support bundle without camera credentials or footage. */
export function DiagnosticsSettings() {
  const client = useQueryClient();
  const capture = useQuery({ queryKey: ["diagnostics"], queryFn: ({ signal }) => api<Capture>("/api/diagnostics/status", { signal }), refetchInterval: 30_000 });
  const [hours, setHours] = useState(4);
  const [error, setError] = useState<string | null>(null);

  async function set(enabled: boolean) {
    setError(null);
    try {
      const result = await api<Capture>("/api/diagnostics/capture", { method: "POST", body: { enabled, hours } });
      client.setQueryData(["diagnostics"], result);
    } catch (failure) {
      setError(failureText(failure));
    }
  }

  const data = capture.data;
  return (
    <>
    <LiveStartupTimes />
    <section className="field-group" aria-label="Support diagnostics">
      <h2 className="field-heading">Support diagnostics</h2>
      {data ? (
        <>
          <p role="status">
            {data.enabled ? `Capturing until ${formatRelative(data.capture_until)}.` : "Not capturing."} {data.event_count} of at most {data.max_events} events stored; events are kept {data.retention_days} days.
          </p>
          <div className="field-actions">
            {data.enabled ? (
              <button type="button" className="button" onClick={() => void set(false)}>Stop capturing</button>
            ) : (
              <>
                <label className="inline-field">Capture for <input type="number" min={1} max={24} value={hours} onChange={event => setHours(Number(event.target.value))} /> hours</label>
                <button type="button" className="button" data-variant="primary" onClick={() => void set(true)} disabled={!(hours >= 1 && hours <= 24)}>Start capturing</button>
              </>
            )}
            <a className="button" href="/api/diagnostics/bundle" download>Download support bundle</a>
          </div>
          {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
          <p className="muted field-hint">The bundle contains timings, counts and error categories. It never includes camera addresses, credentials or footage.</p>
        </>
      ) : <p className="muted">{capture.error ? failureText(capture.error) : "Loading…"}</p>}
    </section>
    </>
  );
}
