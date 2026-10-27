import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { keys } from "@/api/queries";
import { attentionText, BULK_LABEL, BULK_LIMIT, EMPTY_QUICK, failureText, inspectCamera, parseBulk, runLimited, saveChecked, suggestedName, type BulkRow, type BulkStatus } from "./inspect";

/** At most this many cameras are checked at once, however long the list. The server limits inspections too. */
const CONCURRENCY = 3;
const EXAMPLE = "# address,username,password,name (the name is optional; put a password with a comma in double quotes)\n";

interface Outcome { status: BulkStatus; detail: string }

/** Add many cameras from a list of addresses and logins: each is checked and added in turn, with its own progress and result. */
export function BulkAdd() {
  const client = useQueryClient();
  const [text, setText] = useState(EXAMPLE);
  const [rows, setRows] = useState<BulkRow[]>([]);
  const [outcomes, setOutcomes] = useState<Record<number, Outcome>>({});
  const [running, setRunning] = useState(false);
  const [addAnyway, setAddAnyway] = useState(true);
  const stop = useRef(false);
  const parsed = parseBulk(text);
  const valid = parsed.filter(row => !row.problem);
  const shown = rows.length ? rows : parsed;
  const tooMany = parsed.length > BULK_LIMIT;
  const statusOf = (row: BulkRow): Outcome => outcomes[row.row] ?? (row.problem ? { status: "invalid", detail: row.problem } : { status: "queued", detail: "" });
  const counts = shown.reduce<Record<string, number>>((total, row) => { const key = statusOf(row).status; total[key] = (total[key] ?? 0) + 1; return total; }, {});
  const added = (counts.added ?? 0) + (counts.attention ?? 0);

  function note(row: BulkRow, status: BulkStatus, detail = "") {
    setOutcomes(current => ({ ...current, [row.row]: { status, detail } }));
  }

  async function handle(row: BulkRow) {
    const connection = { ...EMPTY_QUICK, host: row.host, username: row.username, password: row.password };
    note(row, "checking");
    try {
      const report = await inspectCamera(connection);
      if (!report.ok && !addAnyway) {
        note(row, "failed", `Not added because it needs attention: ${attentionText(report)}`);
        return;
      }
      note(row, "adding");
      const camera = await saveChecked(connection, report, row.name || suggestedName(report, row.host));
      note(row, report.ok ? "added" : "attention", report.ok ? camera.name : `${camera.name}: ${attentionText(report)}`);
    } catch (error) {
      note(row, "failed", failureText(error, "The NVR could not be reached"));
    }
  }

  async function run(only?: BulkRow[]) {
    const todo = only ?? parsed.filter(row => !row.problem);
    stop.current = false;
    setRunning(true);
    setRows(parsed);
    setOutcomes(current => (only ? current : {}));
    await runLimited(todo, CONCURRENCY, handle, () => stop.current);
    // Anything not reached because of Stop is marked, so the list says what was and was not done.
    setOutcomes(current => {
      const next = { ...current };
      for (const row of todo) if (!next[row.row] || next[row.row]?.status === "queued") next[row.row] = { status: "skipped", detail: "Stopped before this camera" };
      return next;
    });
    setRunning(false);
    await client.invalidateQueries({ queryKey: keys.cameras });
  }

  const failed = shown.filter(row => ["failed", "skipped"].includes(statusOf(row).status) && !row.problem);

  return (
    <div className="add-layout bulk-add">
      <section className="add-list" aria-label="Bulk add">
        <div className="tab-form">
          <label className="field"><span>Cameras (one per line: address, username, password, optional name)</span>
            <textarea className="mono csv-input" rows={6} value={text} spellCheck={false} disabled={running} onChange={event => { setText(event.target.value); setRows([]); setOutcomes({}); }} />
          </label>
          <label className="inline-field"><input type="checkbox" checked={addAnyway} disabled={running} onChange={event => setAddAnyway(event.target.checked)} /> Also add cameras that need attention (recording stays off)</label>
          <div className="field-actions">
            <button type="button" className="button" data-variant="primary" disabled={running || !valid.length || tooMany} onClick={() => void run()}>
              {running ? "Adding…" : `Check and add ${valid.length} camera${valid.length === 1 ? "" : "s"}`}
            </button>
            {running ? <button type="button" className="button" onClick={() => { stop.current = true; }}>Stop</button> : null}
            {!running && failed.length ? <button type="button" className="button" onClick={() => void run(failed)}>Retry {failed.length} not added</button> : null}
          </div>
          <p className="muted field-hint">Passwords stay in this page only while it is open. {CONCURRENCY} cameras are checked at a time. New cameras start with recording turned off.</p>
          {tooMany ? <p className="field-error" role="alert">Lists are limited to {BULK_LIMIT} cameras. Split the list and add it in parts.</p> : null}
          {shown.length ? (
            <p className="notice" data-tone={counts.failed || counts.invalid ? "warn" : "good"} role="status" aria-live="polite">
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
                {shown.map(row => {
                  const outcome = statusOf(row);
                  return (
                    <tr key={row.row} data-status={outcome.status}>
                      <td>{row.row}</td>
                      <td className="mono">{row.host || <span className="muted">(none)</span>}</td>
                      <td>{BULK_LABEL[outcome.status]}{outcome.detail ? <span className="muted"> · {outcome.detail}</span> : null}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      <section className="add-detail" aria-label="About bulk add">
        <div className="tab-form">
          <h2 className="setup-title">How it works</h2>
          <p className="muted">Each camera is identified, signed in to, and its video checked, then added. A camera whose video could not be verified is added with recording off (when the box is ticked) and the NVR keeps checking it. Nothing is added twice: a name already in use is reported for that row.</p>
        </div>
      </section>
    </div>
  );
}
