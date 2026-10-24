import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";

interface Capture { enabled: boolean; capture_until: string | null; event_count: number; max_events: number; retention_days: number }

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
  );
}
