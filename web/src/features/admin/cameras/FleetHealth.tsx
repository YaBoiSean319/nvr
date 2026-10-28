import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";

interface Row {
  camera_id: string; name: string; group: string | null; recording_status: string;
  manufacturer: string | null; model: string | null; firmware: string | null; firmware_status: "outdated" | "current" | "uniform" | null; newest_firmware: string | null;
  uptime_seconds: number | null;
  configured: { codec: string | null; width: number | null; height: number | null; fps: number | null; bitrate_kbps: number | null };
  measured: { fps: number | null; bitrate_kbps: number | null; at: string | null };
  dropped_frames: number | null; reconnects: number | null; onvif_contact_at: string | null; onvif_error: string | null; onvif_failed: boolean;
}
interface Fleet {
  generated_at: string; cameras: Row[]; outdated: number;
  models: { manufacturer: string | null; model: string | null; cameras: number; newest: string | null; outdated: number; firmwares: { version: string; cameras: number }[] }[];
}

type SortKey = "name" | "model" | "firmware" | "uptime" | "bitrate" | "fps" | "dropped" | "reconnects" | "contact";
const NOT_REPORTED = "Not reported";

function duration(seconds: number | null): string {
  if (seconds === null) return NOT_REPORTED;
  const days = Math.floor(seconds / 86_400), hours = Math.floor((seconds % 86_400) / 3_600), minutes = Math.floor((seconds % 3_600) / 60);
  return days ? `${days} d ${hours} h` : hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}

function pair(configured: number | null, measured: number | null, unit: string): string {
  const show = (value: number | null) => (value === null ? NOT_REPORTED : `${Math.round(value * 10) / 10} ${unit}`);
  return `${show(configured)} set · ${show(measured)} seen`;
}

const KEYS: Record<SortKey, (row: Row) => string | number | null> = {
  name: row => row.name.toLowerCase(),
  model: row => [row.manufacturer, row.model].filter(Boolean).join(" ").toLowerCase() || null,
  firmware: row => row.firmware,
  uptime: row => row.uptime_seconds,
  bitrate: row => row.measured.bitrate_kbps,
  fps: row => row.measured.fps,
  dropped: row => row.dropped_frames,
  reconnects: row => row.reconnects,
  contact: row => row.onvif_contact_at,
};

/** A fleet view of camera health, from values the NVR has stored: it starts no video. Sortable and filterable, and exportable. */
export function FleetHealth() {
  const fleet = useQuery({ queryKey: ["camera-health"], queryFn: ({ signal }) => api<Fleet>("/api/camera-health", { signal }), refetchInterval: 30_000 });
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({ key: "name", descending: false });
  const [search, setSearch] = useState("");
  const [model, setModel] = useState("");
  const [outdatedOnly, setOutdatedOnly] = useState(false);

  const models = useMemo(() => [...new Set((fleet.data?.cameras ?? []).map(row => row.model).filter((value): value is string => Boolean(value)))].sort(), [fleet.data]);
  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const shown = (fleet.data?.cameras ?? []).filter(row => (!needle || `${row.name} ${row.group ?? ""} ${row.manufacturer ?? ""} ${row.model ?? ""} ${row.firmware ?? ""}`.toLowerCase().includes(needle))
      && (!model || row.model === model) && (!outdatedOnly || row.firmware_status === "outdated"));
    const key = KEYS[sort.key];
    // Values that were not reported go last whichever way it is sorted.
    return shown.sort((a, b) => {
      const left = key(a), right = key(b);
      if (left === null || right === null) return left === right ? 0 : left === null ? 1 : -1;
      return (left < right ? -1 : left > right ? 1 : 0) * (sort.descending ? -1 : 1);
    });
  }, [fleet.data, search, model, outdatedOnly, sort]);

  const heading = (key: SortKey, label: string) => (
    <th scope="col" aria-sort={sort.key === key ? (sort.descending ? "descending" : "ascending") : "none"}>
      <button type="button" className="row-select" onClick={() => setSort(value => ({ key, descending: value.key === key && !value.descending }))}>{label}{sort.key === key ? (sort.descending ? " ▼" : " ▲") : ""}</button>
    </th>
  );

  return (
    <section className="cases fleet" aria-labelledby="fleet-title">
      <header className="cases-head">
        <h1 id="fleet-title">Camera health</h1>
        <a className="button" data-variant="ghost" href="/api/camera-health/export.csv" download="camera-health.csv">Download CSV</a>
      </header>
      <p className="muted">What each camera reports and what the NVR has measured. These are stored values, refreshed every few minutes; nothing here opens a camera's video. {NOT_REPORTED} means the camera or recorder gave no value.</p>
      {fleet.error ? <p className="notice" data-tone="bad" role="alert">{failureText(fleet.error, "Camera health could not be loaded.")}</p> : null}
      {fleet.data && fleet.data.outdated ? (
        <p className="notice" data-tone="warn" role="status" data-testid="fleet-outdated">
          {fleet.data.outdated} camera{fleet.data.outdated === 1 ? " is" : "s are"} on older firmware than others of the same model:{" "}
          {fleet.data.models.filter(item => item.outdated).map(item => `${[item.manufacturer, item.model].filter(Boolean).join(" ")} (${item.newest ? `newest ${item.newest}, ` : ""}${item.outdated} behind)`).join("; ")}.
        </p>
      ) : null}
      <div className="insights-controls" role="group" aria-label="Filter cameras">
        <label className="field"><span>Search</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} /></label>
        <label className="field"><span>Model</span>
          <select value={model} onChange={event => setModel(event.target.value)}><option value="">All models</option>{models.map(item => <option key={item} value={item}>{item}</option>)}</select>
        </label>
        <label className="check"><input type="checkbox" checked={outdatedOnly} onChange={event => setOutdatedOnly(event.target.checked)} /> Older firmware only</label>
      </div>
      {fleet.isPending ? <p className="muted">Loading…</p> : null}
      {fleet.data ? <p className="muted" role="status">{rows.length} of {fleet.data.cameras.length} cameras</p> : null}
      <div className="inventory-scroll">
        <table className="inventory-table" aria-label="Camera health">
          <thead>
            <tr>
              {heading("name", "Camera")}{heading("model", "Make and model")}{heading("firmware", "Firmware")}{heading("uptime", "Recording uptime")}
              {heading("bitrate", "Bitrate")}{heading("fps", "Frame rate")}{heading("dropped", "Dropped frames")}{heading("reconnects", "Reconnects")}{heading("contact", "Last ONVIF contact")}
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.camera_id} data-camera-id={row.camera_id} data-firmware-status={row.firmware_status ?? undefined}>
                <th scope="row">{row.name}{row.group ? <span className="muted"> · {row.group}</span> : null}</th>
                <td>{[row.manufacturer, row.model].filter(Boolean).join(" ") || NOT_REPORTED}</td>
                <td>
                  {row.firmware ?? NOT_REPORTED}
                  {row.firmware_status === "outdated" ? <span className="notice" data-tone="warn" data-testid="outdated-badge" title={row.newest_firmware ? `Newest of this model: ${row.newest_firmware}` : "A newer version runs on a camera you cannot see"}> {row.newest_firmware ? `Older than ${row.newest_firmware}` : "Older than others of its model"}</span> : null}
                </td>
                <td>{duration(row.uptime_seconds)}</td>
                <td title={row.measured.at ? `Seen over the half hour to ${row.measured.at}` : undefined}>{pair(row.configured.bitrate_kbps, row.measured.bitrate_kbps, "kbit/s")}</td>
                <td>{pair(row.configured.fps, row.measured.fps, "fps")}</td>
                <td>{row.dropped_frames ?? NOT_REPORTED}</td>
                <td>{row.reconnects ?? NOT_REPORTED}</td>
                <td title={row.onvif_error ?? undefined}>{row.onvif_contact_at ? formatRelative(row.onvif_contact_at) : NOT_REPORTED}{row.onvif_failed ? <span className="muted"> · last try failed</span> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
