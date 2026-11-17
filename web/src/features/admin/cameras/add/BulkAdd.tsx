import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { keys } from "@/api/queries";
import { latestBulkJob, BULK_LABEL, BULK_LIMIT, cancelBulkJob, failureText, getBulkJob, parseBulk, startBulkJob, type BulkJob, type BulkRow, type BulkStatus } from "./inspect";

const EXAMPLE = "# address,username,password,name (the name is optional; put a password with a comma in double quotes)\n";
const POLL_MS = 1500;

interface Shown { row: number; host: string; status: BulkStatus; detail: string }

/** Add many cameras from a list of addresses and logins. The NVR checks and adds them in the background, a few at a time, so this page only shows the progress: it can be closed and opened again. */
export function BulkAdd() {
  const client = useQueryClient();
  const [text, setText] = useState(EXAMPLE);
  const [job, setJob] = useState<BulkJob | null>(null);
  const [sent, setSent] = useState<BulkRow[]>([]);  // The rows of the list that was sent, with their passwords, for Retry. Empty when the job was picked up again after the page was opened.
  const [error, setError] = useState<string | null>(null);
  const [addAnyway, setAddAnyway] = useState(true);
  const [recording, setRecording] = useState(false);
  const running = job?.state === "running";
  const wasRunning = useRef(false);
  const parsed = parseBulk(text);
  const valid = parsed.filter(row => !row.problem);
  const tooMany = parsed.length > BULK_LIMIT;

  // The latest job (still running, or finished within the last hour) is shown again when this page is opened.
  useEffect(() => {
    let live = true;
    void latestBulkJob().then(found => { if (live && found.job) setJob(found.job); }).catch(() => undefined);
    return () => { live = false; };
  }, []);

  // While it runs, ask for its progress. A failed request is shown and tried again; the job itself does not depend on this page.
  useEffect(() => {
    if (!job || job.state !== "running") return undefined;
    let live = true;
    const timer = window.setInterval(() => {
      void getBulkJob(job.id).then(next => { if (live) { setJob(next); setError(null); } }).catch(failure => { if (live) setError(failureText(failure, "The NVR could not be reached")); });
    }, POLL_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [job?.id, job?.state]);

  useEffect(() => {
    if (wasRunning.current && !running) void client.invalidateQueries({ queryKey: keys.cameras });
    wasRunning.current = running;
  }, [running, client]);

  // What the table shows: the server's cameras (numbered as in the list that was sent), plus the lines that were refused here and never sent.
  const original = (serverRow: number) => sent[serverRow - 1]?.row ?? serverRow;
  const shown: Shown[] = job
    ? [
        ...job.items.map(item => ({ row: original(item.row), host: item.host, status: item.status as BulkStatus, detail: item.detail })),
        ...(sent.length ? parsed.filter(row => row.problem).map(row => ({ row: row.row, host: row.host, status: "invalid" as BulkStatus, detail: row.problem ?? "" })) : []),
      ].sort((a, b) => a.row - b.row)
    : parsed.map(row => (row.problem ? { row: row.row, host: row.host, status: "invalid" as BulkStatus, detail: row.problem } : { row: row.row, host: row.host, status: "queued" as BulkStatus, detail: "" }));
  const counts = shown.reduce<Record<string, number>>((total, row) => { total[row.status] = (total[row.status] ?? 0) + 1; return total; }, {});
  const added = (counts.added ?? 0) + (counts.attention ?? 0);
  const retryable = sent.length ? (job?.items ?? []).filter(item => ["failed", "skipped"].includes(item.status)).map(item => sent[item.row - 1]).filter((row): row is BulkRow => Boolean(row)) : [];

  async function start(rows: BulkRow[]) {
    setError(null);
    try {
      const created = await startBulkJob(rows, addAnyway, recording);
      setSent(rows);
      setJob(created);
    } catch (failure) {
      setError(failureText(failure, "The NVR could not be reached"));
    }
  }

  async function stop() {
    if (!job) return;
    try { setJob(await cancelBulkJob(job.id)); } catch (failure) { setError(failureText(failure, "The NVR could not be reached")); }
  }

  return (
    <div className="add-layout bulk-add">
      <section className="add-list" aria-label="Bulk add">
        <div className="tab-form">
          <label className="field"><span>Cameras (one per line: address, username, password, optional name)</span>
            <textarea className="mono csv-input" rows={6} value={text} spellCheck={false} disabled={running} onChange={event => { setText(event.target.value); setJob(null); setSent([]); setError(null); }} />
          </label>
          <label className="inline-field"><input type="checkbox" checked={addAnyway} disabled={running} onChange={event => setAddAnyway(event.target.checked)} /> Also add cameras that need attention (recording stays off)</label>
          <label className="inline-field"><input type="checkbox" checked={recording} disabled={running} onChange={event => setRecording(event.target.checked)} /> Turn on motion recording for cameras that check out completely</label>
          <div className="field-actions">
            <button type="button" className="button" data-variant="primary" disabled={running || !valid.length || tooMany} onClick={() => void start(valid)}>
              {running ? "Adding…" : `Check and add ${valid.length} camera${valid.length === 1 ? "" : "s"}`}
            </button>
            {running ? <button type="button" className="button" onClick={() => void stop()}>Stop</button> : null}
            {!running && retryable.length ? <button type="button" className="button" onClick={() => void start(retryable)}>Retry {retryable.length} not added</button> : null}
          </div>
          <p className="muted field-hint">The NVR works through the list in the background, three cameras at a time, so you can close this page and come back: the list and its results will be here. Each password is kept, encrypted, only until that camera has been checked. New cameras start with recording turned off.</p>
          {tooMany ? <p className="field-error" role="alert">Lists are limited to {BULK_LIMIT} cameras. Split the list and add it in parts.</p> : null}
          {error ? <p className="field-error" role="alert">{error}</p> : null}
          {shown.length ? (
            <p className="notice" data-tone={counts.failed || counts.invalid ? "warn" : "good"} role="status" aria-live={running ? "off" : "polite"}>
              {added} added of {shown.length}
              {counts.attention ? ` (${counts.attention} need attention)` : ""}
              {counts.failed ? `, ${counts.failed} not added` : ""}
              {counts.invalid ? `, ${counts.invalid} invalid` : ""}
              {running ? `, ${(counts.checking ?? 0) + (counts.adding ?? 0)} in progress` : ""}.
            </p>
          ) : null}
        </div>
        {shown.length ? (
          <div className="inventory-scroll csv-results">
            <table className="inventory-table" aria-label="Bulk add results">
              <thead><tr><th scope="col">Row</th><th scope="col">Address</th><th scope="col">Result</th></tr></thead>
              <tbody>
                {shown.map(row => (
                  <tr key={row.row} data-status={row.status}>
                    <td>{row.row}</td>
                    <td className="mono">{row.host || <span className="muted">(none)</span>}</td>
                    <td>{BULK_LABEL[row.status]}{row.detail ? <span className="muted"> · {row.detail}</span> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      <section className="add-detail" aria-label="How bulk adding works">
        <div className="tab-form">
          <h2 className="setup-title">How it works</h2>
          <p className="muted">Each camera is identified, signed in to, and its video checked, then added. A camera whose video could not be verified is added with recording off (when the box is ticked) and the NVR keeps checking it. A name already in use is reported for that row, so running the same list again does not add the cameras twice.</p>
        </div>
      </section>
    </div>
  );
}
