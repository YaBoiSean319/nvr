import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { useCameras, useDisplaySettings } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { formatSiteDateTime } from "@/lib/zonedTime";

type Unit = "hour" | "day";
type LabelFilter = "" | "person" | "vehicle";

interface Subject { id: string; kind: "camera" | "zone" | "line"; name: string; camera_id: string; camera_name: string }
interface Report {
  unit: Unit;
  labels: string[];
  shown_labels: string[];
  buckets: { start: string; end: string }[];
  subjects: Subject[];
  counts: Record<string, number[][]>;
  /** Per camera and bucket: the share of it the AI analysed, or null for time that has not happened. */
  analysed: Record<string, (number | null)[]>;
  coverage: { fully_analysed: boolean; analysed_percent: number | null };
}
interface Heat { columns: number; rows: number; cells: number[][]; peak: number; tracks: number; coverage: { fully_analysed: boolean } }

const RANGES = [
  { id: "24h", label: "Last 24 hours", hours: 24, unit: "hour" as Unit },
  { id: "7d", label: "Last 7 days", hours: 24 * 7, unit: "day" as Unit },
  { id: "30d", label: "Last 30 days", hours: 24 * 30, unit: "day" as Unit },
];
const KIND_LABEL = { camera: "Camera", zone: "Zone", line: "Line" } as const;
const CHART = { width: 720, height: 180, left: 36, bottom: 22, top: 8 };

function periodOf(rangeId: string): { start: string; end: string } {
  const range = RANGES.find(item => item.id === rangeId) ?? RANGES[0]!;
  const end = new Date();
  end.setMinutes(0, 0, 0);
  end.setHours(end.getHours() + 1); // include the hour in progress
  return { start: new Date(end.getTime() - range.hours * 3_600_000).toISOString(), end: end.toISOString() };
}

function Chart({ report, subject, format }: { report: Report; subject: Subject; format: (iso: string) => string }) {
  const cells = report.counts[subject.id] ?? [];
  const analysed = report.analysed[subject.camera_id] ?? [];
  const totals = cells.map(cell => cell.reduce((sum, value) => sum + value, 0));
  const peak = Math.max(1, ...totals);
  const slot = (CHART.width - CHART.left) / Math.max(1, cells.length);
  const plot = CHART.height - CHART.bottom - CHART.top;
  const people = report.labels.indexOf("person");
  return (
    <figure className="insights-chart">
      <svg viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label={`${subject.name}: people and vehicles per ${report.unit}`} data-testid="insights-chart">
        <line x1={CHART.left} x2={CHART.width} y1={CHART.height - CHART.bottom} y2={CHART.height - CHART.bottom} className="chart-axis" />
        <text x={CHART.left - 4} y={CHART.top + 8} textAnchor="end" className="chart-tick">{peak}</text>
        <text x={CHART.left - 4} y={CHART.height - CHART.bottom} textAnchor="end" className="chart-tick">0</text>
        {cells.map((cell, index) => {
          const percent = analysed[index];
          const partial = percent !== null && percent !== undefined && percent < 99.95;
          const bucket = report.buckets[index]!;
          const x = CHART.left + index * slot;
          const person = (cell[people] ?? 0) / peak * plot;
          const vehicle = (cell[1 - people] ?? 0) / peak * plot;
          const base = CHART.height - CHART.bottom;
          return (
            <g key={bucket.start} data-bucket={bucket.start} data-total={totals[index]} data-analysed={percent ?? "future"}>
              <title>{`${format(bucket.start)}: ${cell[people] ?? 0} people, ${cell[1 - people] ?? 0} vehicles${percent === null || percent === undefined ? " (not yet)" : partial ? ` (only ${percent}% analysed: counts may be lower than what happened)` : ""}`}</title>
              {partial || percent === null || percent === undefined ? <rect x={x} y={CHART.top} width={Math.max(1, slot - 1)} height={plot} className="chart-unanalysed" data-testid="unanalysed-mark" /> : null}
              <rect x={x + 1} y={base - person} width={Math.max(1, slot - 3)} height={person} className="chart-person" />
              <rect x={x + 1} y={base - person - vehicle} width={Math.max(1, slot - 3)} height={vehicle} className="chart-vehicle" />
            </g>
          );
        })}
      </svg>
      <figcaption className="muted">
        <span className="legend-swatch chart-person" /> People <span className="legend-swatch chart-vehicle" /> Vehicles <span className="legend-swatch chart-unanalysed" /> Not fully analysed, so counts may be lower than what happened
      </figcaption>
    </figure>
  );
}

function Heatmap({ cameraId, period, label }: { cameraId: string; period: { start: string; end: string }; label: LabelFilter }) {
  const params = new URLSearchParams({ camera_id: cameraId, ...period, ...(label ? { label } : {}) });
  const heat = useQuery({ queryKey: ["insights-heat", cameraId, period, label], queryFn: ({ signal }) => api<Heat>(`/api/insights/heatmap?${params}`, { signal }) });
  const [noFrame, setNoFrame] = useState(false);
  const data = heat.data;
  return (
    <section className="insights-heat" aria-label="Heatmap">
      {heat.error ? <p className="notice" data-tone="bad" role="alert">{failureText(heat.error, "The heatmap could not be loaded.")}</p> : null}
      {data ? (
        <>
          <div className="heat-frame" data-testid="heatmap">
            {noFrame ? null : <img src={`/api/insights/reference?camera_id=${encodeURIComponent(cameraId)}`} alt="" onError={() => setNoFrame(true)} />}
            <svg viewBox={`0 0 ${data.columns} ${data.rows}`} preserveAspectRatio="none" aria-hidden="true">
              {data.cells.flatMap((row, y) => row.map((value, x) => (value ? (
                <rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} className="heat-cell" data-testid="heat-cell" data-count={value} style={{ opacity: Math.max(0.12, Math.sqrt(value / data.peak)) }} />
              ) : null)))}
            </svg>
          </div>
          <p className="muted">
            {data.tracks ? `${data.tracks} object${data.tracks === 1 ? "" : "s"} seen; brighter means more time spent there.` : "Nothing was seen in this period."}
            {noFrame ? " There is no recording to show behind it." : ""}
            {data.coverage.fully_analysed ? "" : " Some of this period was not analysed, so it shows less than happened."}
          </p>
        </>
      ) : heat.isPending ? <p className="muted">Loading the heatmap…</p> : null}
    </section>
  );
}

/** Insights: counts of people and vehicles per camera, zone and line, where objects go, and a CSV of the counts. */
export function InsightsWorkspace() {
  const cameras = useCameras();
  const display = useDisplaySettings();
  const [rangeId, setRangeId] = useState("24h");
  const [unit, setUnit] = useState<Unit>("hour");
  const [label, setLabel] = useState<LabelFilter>("");
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [subjectId, setSubjectId] = useState<string | null>(null);
  const [heatCamera, setHeatCamera] = useState<string | null>(null);
  const period = useMemo(() => periodOf(rangeId), [rangeId]);
  const active = (cameras.data ?? []).filter(camera => !camera.archived_at);
  const selected = chosen ?? active.map(camera => camera.id);
  const format = (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);

  const query = new URLSearchParams({ ...period, unit, ...(label ? { label } : {}) });
  for (const id of selected) query.append("camera_ids", id);
  const report = useQuery({
    queryKey: ["insights", period, unit, label, selected],
    enabled: Boolean(cameras.data) && selected.length > 0,
    queryFn: ({ signal }) => api<Report>(`/api/insights?${query}`, { signal }),
  });
  const data = report.data;
  const subject = data?.subjects.find(item => item.id === subjectId) ?? data?.subjects[0] ?? null;
  const heatId = heatCamera && selected.includes(heatCamera) ? heatCamera : selected[0] ?? null;

  function pickRange(id: string) {
    setRangeId(id);
    setUnit(RANGES.find(item => item.id === id)?.unit ?? "hour");
  }
  const total = (id: string, index: number) => (data?.counts[id] ?? []).reduce((sum, cell) => sum + (cell[index] ?? 0), 0);

  return (
    <section className="insights" aria-labelledby="insights-title">
      <header className="cases-head">
        <h1 id="insights-title">Insights</h1>
        <a className="button" data-variant="ghost" href={`/api/insights/export.csv?${query}`} download="insights.csv">Download CSV</a>
      </header>
      <div className="insights-controls" role="group" aria-label="What to count">
        <label className="field"><span>Period</span>
          <select value={rangeId} onChange={event => pickRange(event.target.value)}>{RANGES.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
        </label>
        <label className="field"><span>Count by</span>
          <select value={unit} onChange={event => setUnit(event.target.value as Unit)}>
            <option value="hour" disabled={rangeId === "30d"}>Hour</option><option value="day">Day</option>
          </select>
        </label>
        <label className="field"><span>Show</span>
          <select value={label} onChange={event => setLabel(event.target.value as LabelFilter)}>
            <option value="">People and vehicles</option><option value="person">People</option><option value="vehicle">Vehicles</option>
          </select>
        </label>
        <fieldset className="insights-cameras"><legend>Cameras</legend>
          {active.map(camera => (
            <label key={camera.id} className="check">
              <input type="checkbox" checked={selected.includes(camera.id)}
                     onChange={event => setChosen(event.target.checked ? [...selected, camera.id] : selected.filter(id => id !== camera.id))} />
              {camera.name}
            </label>
          ))}
        </fieldset>
      </div>
      {report.error ? <p className="notice" data-tone="bad" role="alert">{failureText(report.error, "Insights could not be loaded.")}</p> : null}
      {!selected.length ? <p className="muted">Choose at least one camera.</p> : null}
      {report.isPending && selected.length ? <p className="muted">Counting…</p> : null}
      {data ? (
        <>
          {data.coverage.fully_analysed ? null : (
            <p className="notice" data-tone="warn" role="status" data-testid="insights-unanalysed">
              The AI was not analysing all of this time{data.coverage.analysed_percent !== null ? ` (${data.coverage.analysed_percent}% was)` : ""}. Hours that were not fully analysed are marked, and their counts may be lower than what happened.
            </p>
          )}
          <table className="insights-table" aria-label="Totals for the period">
            <thead><tr><th>Name</th><th>Kind</th><th>Camera</th>{data.labels.map(name => (data.shown_labels.includes(name) ? <th key={name} className="num">{name === "person" ? "People" : "Vehicles"}</th> : null))}</tr></thead>
            <tbody>
              {data.subjects.map(item => (
                <tr key={item.id} data-subject={item.id} data-current={item.id === subject?.id}>
                  <td><button type="button" className="row-select" onClick={() => setSubjectId(item.id)} aria-pressed={item.id === subject?.id}>{item.name}</button></td>
                  <td>{KIND_LABEL[item.kind]}</td>
                  <td>{item.kind === "camera" ? "" : item.camera_name}</td>
                  {data.labels.map((name, index) => (data.shown_labels.includes(name) ? <td key={name} className="num" data-label={name}>{total(item.id, index)}</td> : null))}
                </tr>
              ))}
            </tbody>
          </table>
          {subject ? <Chart report={data} subject={subject} format={format} /> : null}
        </>
      ) : null}
      {heatId ? (
        <>
          <h2>Where objects go</h2>
          <label className="field"><span>Camera</span>
            <select aria-label="Heatmap camera" value={heatId} onChange={event => setHeatCamera(event.target.value)}>{active.filter(camera => selected.includes(camera.id)).map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}</select>
          </label>
          <Heatmap cameraId={heatId} period={period} label={label} />
        </>
      ) : null}
    </section>
  );
}
