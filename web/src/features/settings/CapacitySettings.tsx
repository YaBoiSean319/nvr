import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

type Codec = "h264" | "h265";
type Recording = "continuous" | "motion" | "disabled";
interface PlanRow {
  name: string; count: number; width: number | null; height: number | null; codec: Codec | null; fps: number | null; bitrate_kbps: number | null; bitrate_source: "entered" | "measured" | "assumed";
  recording: Recording; recorded_share: number | null; retention_days: number; ai: boolean; analysis_fps: number | null;
}
interface Metric { label: string; value: number; unit: string; basis: string; assumes: string[] }
interface Line { name: string; count: number; bitrate_kbps: number; bitrate_source: string; duty: number; bytes_per_day: number; footage_bytes: number; assumed: string[] }
interface AssumptionResult { key: string; label: string; value: number; unit: string; source: "default" | "entered"; used: boolean }
interface Estimate { cameras: number; lines: Line[]; metrics: Record<string, Metric>; assumptions: AssumptionResult[]; disclaimer: string }
interface AssumptionInfo { key: string; label: string; value: number; unit: string; low: number; high: number }

const PICTURES: [string, number, number][] = [["640 x 480", 640, 480], ["1280 x 720 (720p)", 1280, 720], ["1920 x 1080 (1080p)", 1920, 1080], ["2560 x 1440 (4 MP)", 2560, 1440], ["3072 x 1728 (5 MP)", 3072, 1728], ["3840 x 2160 (4K)", 3840, 2160]];
const BLANK: PlanRow = { name: "Cameras", count: 10, width: 1920, height: 1080, codec: "h264", fps: 15, bitrate_kbps: null, bitrate_source: "entered", recording: "continuous", recorded_share: null, retention_days: 30, ai: false, analysis_fps: null };
const ORDER = ["disk_footage", "disk_needed", "disk_recommended", "write_average", "write_peak", "network_in", "network_out", "cores_streams", "cores_conversion", "cores_ai_processor", "cores_ai_accelerated", "cores_total_processor", "cores_total_accelerated"];

function format(metric: Metric): string {
  if (metric.unit === "bytes") {
    const tb = metric.value / 1e12;
    return tb >= 1 ? `${tb.toFixed(2)} TB` : `${(metric.value / 1e9).toFixed(1)} GB`;
  }
  return `${metric.value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${metric.unit}`;
}

function download(name: string, type: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  URL.revokeObjectURL(url);
}
const quote = (text: string) => `"${text.replace(/"/g, '""')}"`;

/** What a set of cameras needs in disk, write speed, network and processing: an estimate, with its assumptions, never a certification. */
export function CapacitySettings() {
  const [rows, setRows] = useState<PlanRow[]>([BLANK]);
  const [viewers, setViewers] = useState(2);
  const [tiles, setTiles] = useState(4);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const info = useQuery({ queryKey: ["admin", "capacity", "assumptions"], queryFn: ({ signal }) => api<{ assumptions: AssumptionInfo[]; disclaimer: string }>("/api/admin/capacity/assumptions", { signal }) });
  const estimate = useMutation({
    mutationFn: (body: { rows: PlanRow[]; viewers: number; tiles_per_viewer: number; assumptions: Record<string, number> }) => api<Estimate>("/api/admin/capacity/estimate", { method: "POST", body }),
    onSuccess: () => setError(null), onError: problem => setError(failureText(problem, "The estimate could not be made.")),
  });
  const inventory = useMutation({
    mutationFn: () => api<{ rows: PlanRow[] }>("/api/admin/capacity/inventory"),
    onSuccess: result => { setError(null); setRows(result.rows.length ? result.rows : [BLANK]); }, onError: problem => setError(failureText(problem, "The cameras could not be loaded.")),
  });
  const valid = rows.every(row => row.count >= 1 && row.retention_days >= 1 && (row.bitrate_kbps === null || row.bitrate_kbps >= 16));
  const request = useMemo(() => ({ rows: rows.map(row => ({ ...row, bitrate_source: row.bitrate_kbps === null ? "entered" as const : row.bitrate_source })), viewers, tiles_per_viewer: tiles, assumptions: overrides }), [rows, viewers, tiles, overrides]);
  useEffect(() => {
    if (!valid) return;
    const timer = window.setTimeout(() => estimate.mutate(request), 400);
    return () => window.clearTimeout(timer);
  }, [request, valid]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (index: number, change: Partial<PlanRow>) => setRows(current => current.map((row, at) => (at === index ? { ...row, ...change, bitrate_source: "bitrate_kbps" in change ? "entered" : row.bitrate_source } : row)));
  const result = estimate.data;
  const labelled = (index: number) => result?.lines[index];

  const exportCsv = () => {
    if (!result) return;
    const lines = [
      ["Capacity estimate (not a certification of capacity)"], [result.disclaimer], [], ["Result", "Value", "Unit", "Basis"],
      ...ORDER.filter(key => result.metrics[key]).map(key => [result.metrics[key]!.label, String(result.metrics[key]!.value), result.metrics[key]!.unit, result.metrics[key]!.basis]), [],
      ["Cameras", "Count", "Bitrate kbps", "Bitrate source", "Share of day recorded", "Bytes a day", "Footage bytes", "Assumed"],
      ...result.lines.map(line => [line.name, String(line.count), String(line.bitrate_kbps), line.bitrate_source, String(line.duty), String(line.bytes_per_day), String(line.footage_bytes), line.assumed.join("; ")]), [],
      ["Assumption", "Value", "Unit", "Source"], ...result.assumptions.map(item => [item.label, String(item.value), item.unit, item.source]),
    ];
    download("capacity-estimate.csv", "text/csv", lines.map(line => line.map(quote).join(",")).join("\n"));
  };
  const exportJson = () => { if (result) download("capacity-estimate.json", "application/json", JSON.stringify({ plan: request, result }, null, 2)); };

  return (
    <>
      <section className="field-group settings-block" aria-label="Capacity planner">
        <h2 className="field-heading">Capacity planner</h2>
        <p className="notice" data-tone="warn" role="note">
          This is an estimate, not a certification of capacity. Real footage varies with the scene, the camera's settings and the hardware: measure with a few cameras before buying for many, and keep headroom.
        </p>
        <p className="muted field-hint">
          Enter the cameras you plan for, or start from the cameras this NVR already has: their bitrates are then the ones measured over the last 48 hours where there is enough footage. Anything you leave empty is
          worked from a stated default and marked <em>assumed</em>.
        </p>
        <div className="field-actions">
          <button type="button" className="button" onClick={() => inventory.mutate()} disabled={inventory.isPending}>Start from current cameras</button>
          <button type="button" className="button" onClick={() => setRows(current => [...current, { ...BLANK, name: `Cameras ${current.length + 1}` }])} disabled={rows.length >= 200}>Add cameras</button>
        </div>
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
        <div className="table-scroll">
          <table className="data-table" aria-label="Cameras in the plan">
            <thead><tr><th>Name</th><th>Count</th><th>Picture</th><th>Codec</th><th>Frames/s</th><th>Bitrate (kbps)</th><th>Recording</th><th>Keep (days)</th><th>AI</th><th /></tr></thead>
            <tbody>
              {rows.map((row, index) => {
                const known = PICTURES.some(([, w, h]) => w === row.width && h === row.height);
                const source = labelled(index)?.bitrate_source;
                return (
                  <tr key={index}>
                    <td><input aria-label={`Name of row ${index + 1}`} value={row.name} maxLength={80} onChange={event => set(index, { name: event.target.value })} /></td>
                    <td><input aria-label={`Cameras in row ${index + 1}`} type="number" min={1} max={5000} value={row.count} onChange={event => set(index, { count: Number(event.target.value) })} /></td>
                    <td>
                      <select aria-label={`Picture size of row ${index + 1}`} value={known ? `${row.width}x${row.height}` : row.width ? "other" : ""}
                              onChange={event => { const [w, h] = event.target.value.split("x").map(Number); set(index, event.target.value === "" ? { width: null, height: null } : { width: w!, height: h! }); }}>
                        <option value="">Not known (1080p assumed)</option>
                        {PICTURES.map(([label, w, h]) => <option key={label} value={`${w}x${h}`}>{label}</option>)}
                        {!known && row.width ? <option value="other">{row.width} x {row.height}</option> : null}
                      </select>
                    </td>
                    <td>
                      <select aria-label={`Codec of row ${index + 1}`} value={row.codec ?? ""} onChange={event => set(index, { codec: (event.target.value || null) as Codec | null })}>
                        <option value="">Not known (H.264 assumed)</option><option value="h264">H.264</option><option value="h265">H.265</option>
                      </select>
                    </td>
                    <td><input aria-label={`Frame rate of row ${index + 1}`} type="number" min={1} max={120} placeholder="15" value={row.fps ?? ""} onChange={event => set(index, { fps: event.target.value === "" ? null : Number(event.target.value) })} /></td>
                    <td>
                      <input aria-label={`Bitrate of row ${index + 1}`} type="number" min={16} placeholder="assumed" value={row.bitrate_kbps ?? ""} onChange={event => set(index, { bitrate_kbps: event.target.value === "" ? null : Number(event.target.value) })} />
                      {source ? <span className="muted" data-testid={`source-${index}`}> {source}</span> : null}
                    </td>
                    <td>
                      <select aria-label={`Recording of row ${index + 1}`} value={row.recording} onChange={event => set(index, { recording: event.target.value as Recording })}>
                        <option value="continuous">Continuous</option><option value="motion">On motion</option><option value="disabled">Not recorded</option>
                      </select>
                    </td>
                    <td><input aria-label={`Days kept for row ${index + 1}`} type="number" min={1} max={3650} value={row.retention_days} onChange={event => set(index, { retention_days: Number(event.target.value) })} /></td>
                    <td><input aria-label={`AI analysis for row ${index + 1}`} type="checkbox" checked={row.ai} onChange={event => set(index, { ai: event.target.checked })} /></td>
                    <td><button type="button" className="button" data-variant="ghost" aria-label={`Remove row ${index + 1}`} disabled={rows.length === 1} onClick={() => setRows(current => current.filter((_, at) => at !== index))}>Remove</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="field-row">
          <label className="field"><span>People watching Live at once</span><input type="number" min={0} max={1000} value={viewers} onChange={event => setViewers(Number(event.target.value))} /></label>
          <label className="field"><span>Cameras each is watching</span><input type="number" min={1} max={64} value={tiles} onChange={event => setTiles(Number(event.target.value))} /></label>
        </div>
        <details>
          <summary>Change the assumptions</summary>
          <div className="field-row">
            {info.data?.assumptions.map(item => (
              <label className="field" key={item.key}>
                <span>{item.label} ({item.unit})</span>
                <input type="number" step="any" min={item.low} max={item.high} value={overrides[item.key] ?? item.value} onChange={event => setOverrides(current => ({ ...current, [item.key]: Number(event.target.value) }))} />
              </label>
            ))}
          </div>
          <button type="button" className="button" data-variant="ghost" onClick={() => setOverrides({})}>Back to the defaults</button>
        </details>
      </section>

      {result ? (
        <section className="field-group settings-block" aria-label="Estimate">
          <h2 className="field-heading">Estimate for {result.cameras} camera{result.cameras === 1 ? "" : "s"}</h2>
          <table className="data-table" aria-label="Estimated needs">
            <thead><tr><th>Need</th><th>Estimate</th><th>How it is worked out</th><th>Assumes</th></tr></thead>
            <tbody>
              {ORDER.filter(key => result.metrics[key]).map(key => {
                const metric = result.metrics[key]!;
                return (
                  <tr key={key} data-metric={key}>
                    <th scope="row">{metric.label}</th><td>{format(metric)}</td><td className="muted">{metric.basis}</td>
                    <td className="muted">{metric.assumes.map(item => result.assumptions.find(entry => entry.key === item)?.label ?? item).join("; ") || "Only what you entered"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="field-actions">
            <button type="button" className="button" onClick={exportCsv}>Download CSV</button>
            <button type="button" className="button" onClick={exportJson}>Download JSON</button>
          </div>
          <section aria-label="Assumptions">
            <h3 className="field-heading">Assumptions behind these figures</h3>
            <ul className="status-list">
              {result.assumptions.filter(item => item.used).map(item => <li key={item.key}><strong>{item.label}</strong><span className="muted">{item.value} {item.unit} ({item.source === "entered" ? "changed by you" : "default"})</span></li>)}
              {result.lines.filter(line => line.assumed.length).map(line => <li key={line.name}><strong>{line.name}</strong><span className="muted">assumed: {line.assumed.join("; ")}</span></li>)}
            </ul>
          </section>
        </section>
      ) : null}
    </>
  );
}
