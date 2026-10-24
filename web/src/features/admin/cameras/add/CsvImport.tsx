import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { keys } from "@/api/queries";
import { failureText } from "../../tabDraft";

interface Row {
  row: number;
  name: string;
  group_name: string | null;
  status: "accepted" | "duplicate" | "invalid" | "failed" | "created";
  errors: string[];
}

const STATUS_LABEL: Record<Row["status"], string> = {
  accepted: "Ready", duplicate: "Duplicate", invalid: "Invalid", failed: "Failed", created: "Added",
};

/** Preview a CSV, then import it; every row reports its own outcome. */
export function CsvImport() {
  const client = useQueryClient();
  const [text, setText] = useState("name,stream_url,recording_mode,group_name\n");
  const [preview, setPreview] = useState<{ rows: Row[]; accepted: number; csv: string } | null>(null);
  const [applied, setApplied] = useState<{ rows: Row[]; created: number; failed: number } | null>(null);
  const [busy, setBusy] = useState<"preview" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedRow, setSelectedRow] = useState<number | null>(null);
  const rows = applied?.rows ?? preview?.rows ?? [];
  const selected = rows.find(row => row.row === selectedRow) ?? null;
  const stale = preview !== null && preview.csv !== text;

  async function runPreview() {
    setBusy("preview");
    setError(null);
    setApplied(null);
    try {
      const result = await api<{ rows: Row[]; accepted_count: number }>("/api/cameras/import/preview", { method: "POST", body: { csv_text: text } });
      setPreview({ rows: result.rows, accepted: result.accepted_count, csv: text });
    } catch (failure) {
      setPreview(null);
      setError(failureText(failure, "The CSV could not be previewed."));
    } finally {
      setBusy(null);
    }
  }

  async function apply() {
    if (!preview) return;
    setBusy("apply");
    setError(null);
    try {
      // Exactly the previewed text is imported; the server validates and tests every row again.
      const result = await api<{ rows: Row[]; created_count: number; failed_count: number }>("/api/cameras/import/apply", { method: "POST", body: { csv_text: preview.csv } });
      // Count from the rows: duplicates are neither created nor counted as failures by the server.
      const created = result.rows.filter(row => row.status === "created").length;
      setApplied({ rows: result.rows, created, failed: result.rows.length - created });
      await client.invalidateQueries({ queryKey: keys.cameras });
    } catch (failure) {
      setError(failureText(failure, "The import could not run."));
    } finally {
      setBusy(null);
    }
  }

  async function loadFile(file: File | undefined) {
    if (file) setText(await file.text());
  }

  return (
    <div className="add-layout">
      <section className="add-list" aria-label="CSV import">
        <div className="tab-form">
          <label className="field"><span>CSV (columns: name, stream_url, and optionally recording_mode, group_name, onvif_uuid)</span>
            <textarea className="mono csv-input" rows={6} value={text} spellCheck={false} onChange={event => setText(event.target.value)} />
          </label>
          <div className="field-actions">
            <label className="button file-button">Choose file<input type="file" accept=".csv,text/csv" onChange={event => void loadFile(event.target.files?.[0])} /></label>
            <button type="button" className="button" onClick={() => void runPreview()} disabled={busy !== null}>{busy === "preview" ? "Checking…" : "Preview import"}</button>
            <button type="button" className="button" data-variant="primary" onClick={() => void apply()} disabled={busy !== null || !preview || stale || !preview.accepted || applied !== null}>
              {busy === "apply" ? "Importing…" : `Import ${preview?.accepted ?? 0} camera${preview?.accepted === 1 ? "" : "s"}`}
            </button>
          </div>
          {stale ? <p className="field-error" role="alert">The CSV changed since the preview. Preview it again before importing.</p> : null}
          {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
          {applied ? <p className="notice" data-tone={applied.failed ? "warn" : "good"} role="status">{applied.created} added, {applied.failed} not added. New cameras start with recording turned off.</p> : null}
          {preview && !applied ? <p className="muted" role="status">{preview.accepted} of {preview.rows.length} rows are ready to import. Each stream is tested during the import.</p> : null}
        </div>
        {rows.length ? (
          <div className="inventory-scroll csv-results">
            <table className="inventory-table" aria-label={applied ? "Import results" : "Import preview"}>
              <thead><tr><th scope="col">Row</th><th scope="col">Name</th><th scope="col">Group</th><th scope="col">Result</th></tr></thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.row} data-status={row.status} aria-selected={row.row === selectedRow} onClick={() => setSelectedRow(row.row)}>
                    <td><button type="button" className="row-select" onClick={() => setSelectedRow(row.row)}>{row.row}</button></td>
                    <td>{row.name || <span className="muted">(no name)</span>}</td>
                    <td>{row.group_name ?? <span className="muted">None</span>}</td>
                    <td>{STATUS_LABEL[row.status]}{row.errors.length ? <span className="muted"> · {row.errors[0]}</span> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
      <section className="add-detail" aria-label="Row details">
        {selected ? (
          <div className="tab-form">
            <h2 className="setup-title">Row {selected.row}: {selected.name || "(no name)"}</h2>
            <p>{STATUS_LABEL[selected.status]}</p>
            {selected.errors.length ? <ul className="plain-list">{selected.errors.map(message => <li key={message}>{message}</li>)}</ul> : <p className="muted">No problems.</p>}
          </div>
        ) : <div className="editor-empty"><p className="muted">Select a row to see its details.</p></div>}
      </section>
    </div>
  );
}
