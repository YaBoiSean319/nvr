import { useDeferredValue, useMemo } from "react";
import type { Camera } from "@/api/types";
import { HEALTH_TONE } from "@/features/cameras/CameraBrowser";
import { guarded } from "../drafts";
import {
  BRAND_LABEL, CONNECTION_LABEL, HEALTH_LABEL, MODE_LABEL, connectionOf, modeOf, visibleCameras,
  type Connection, type SortKey,
} from "./inventory";
import { useCamerasStore } from "./store";

const CONNECTION_TONE: Record<Connection, "good" | "bad" | undefined> = { connected: "good", unreachable: "bad", not_monitored: undefined, unknown: undefined };

interface Props {
  cameras: Camera[] | undefined;
  loading: boolean;
  error: string | null;
  admin: boolean;
}

/** Bounded, searchable inventory. A row selects the editor; checkboxes only choose cameras for a batch. */
export function InventoryTable({ cameras, loading, error, admin }: Props) {
  const filters = useCamerasStore(state => state.filters);
  const sort = useCamerasStore(state => state.sort);
  const showBrand = useCamerasStore(state => state.showBrand);
  const selectedId = useCamerasStore(state => state.selectedId);
  const checked = useCamerasStore(state => state.checked);
  const { setFilters, setSort, setShowBrand, select, toggleChecked, setChecked } = useCamerasStore.getState();
  const query = useDeferredValue(filters.query);
  const rows = useMemo(() => visibleCameras(cameras ?? [], { ...filters, query }, sort), [cameras, filters, query, sort]);
  const groups = useMemo(() => [...new Set((cameras ?? []).map(camera => camera.group_name).filter((name): name is string => Boolean(name)))].sort(), [cameras]);
  const hiddenChecked = checked.filter(id => !rows.some(camera => camera.id === id)).length;
  const allVisibleChecked = rows.length > 0 && rows.every(camera => checked.includes(camera.id));

  function header(key: SortKey, label: string) {
    const active = sort.key === key;
    return (
      <th scope="col" aria-sort={active ? (sort.direction === 1 ? "ascending" : "descending") : "none"}>
        <button type="button" className="sort-button" onClick={() => setSort({ key, direction: active ? (sort.direction === 1 ? -1 : 1) : 1 })}>
          {label}<span aria-hidden="true">{active ? (sort.direction === 1 ? " ▲" : " ▼") : ""}</span>
        </button>
      </th>
    );
  }

  return (
    <section className="inventory" aria-label="Camera inventory">
      <div className="inventory-tools">
        <input type="search" className="browser-search" placeholder="Search name or IP address" aria-label="Search cameras by name or IP address"
               value={filters.query} onChange={event => setFilters({ query: event.target.value })} />
        <select aria-label="Policy group" value={filters.group} onChange={event => setFilters({ group: event.target.value })}>
          <option value="all">All groups</option>
          <option value="__none__">No group</option>
          {groups.map(group => <option key={group} value={group}>{group}</option>)}
        </select>
        <select aria-label="Connection" value={filters.connection} onChange={event => setFilters({ connection: event.target.value as Connection | "all" })}>
          <option value="all">Any connection</option>
          {(Object.keys(CONNECTION_LABEL) as Connection[]).map(value => <option key={value} value={value}>{CONNECTION_LABEL[value]}</option>)}
        </select>
        <select aria-label="Recording health" value={filters.health} onChange={event => setFilters({ health: event.target.value as Camera["recording_health"]["state"] | "all" })}>
          <option value="all">Any recording health</option>
          {(Object.keys(HEALTH_LABEL) as Camera["recording_health"]["state"][]).map(value => <option key={value} value={value}>{HEALTH_LABEL[value]}</option>)}
        </select>
        <select aria-label="Archived cameras" value={filters.archive} onChange={event => setFilters({ archive: event.target.value as "active" | "archived" | "all" })}>
          <option value="active">Active cameras</option>
          <option value="archived">Archived cameras</option>
          <option value="all">Active and archived</option>
        </select>
        <label className="inventory-toggle"><input type="checkbox" checked={showBrand} onChange={event => setShowBrand(event.target.checked)} /> Brand</label>
      </div>
      <div className="inventory-meta" aria-live="polite">
        {rows.length} of {(cameras ?? []).length} cameras
        {admin && checked.length ? (
          <span className="bulk-summary">
            {" · "}{checked.length} selected for bulk changes{hiddenChecked ? ` (${hiddenChecked} hidden by filters)` : ""}
            <button type="button" className="button" data-variant="ghost" onClick={() => setChecked([])}>Clear selection</button>
          </span>
        ) : null}
      </div>
      <div className="inventory-scroll">
        {loading ? <p className="muted inventory-empty">Loading cameras…</p> : null}
        {error ? <p className="notice inventory-empty" data-tone="bad" role="alert">{error}</p> : null}
        {!loading && !error && !(cameras ?? []).length ? <p className="muted inventory-empty">No cameras have been added yet.</p> : null}
        {!loading && !error && (cameras ?? []).length > 0 && !rows.length ? <p className="muted inventory-empty">No cameras match these filters.</p> : null}
        {rows.length ? (
          <table className="inventory-table">
            <thead>
              <tr>
                {admin ? (
                  <th scope="col" className="check-cell">
                    <input type="checkbox" aria-label="Select all shown cameras for bulk changes" checked={allVisibleChecked}
                           onChange={event => setChecked(event.target.checked ? [...new Set([...checked, ...rows.map(camera => camera.id)])] : checked.filter(id => !rows.some(camera => camera.id === id)))} />
                  </th>
                ) : null}
                {header("name", "Name")}
                {header("ip", "IP address")}
                {header("group", "Group")}
                {header("connection", "Connection")}
                {header("health", "Recording health")}
                {header("mode", "Recording mode")}
                {showBrand ? header("brand", "Brand") : null}
              </tr>
            </thead>
            <tbody>
              {rows.map(camera => {
                const connection = connectionOf(camera);
                const selected = camera.id === selectedId;
                return (
                  <tr key={camera.id} aria-selected={selected} data-archived={Boolean(camera.archived_at)}
                      onClick={event => {
                        if ((event.target as HTMLElement).closest("input,button")) return;
                        void guarded(() => select(camera.id));
                      }}>
                    {admin ? (
                      <td className="check-cell">
                        <input type="checkbox" aria-label={`Select ${camera.name} for bulk changes`} checked={checked.includes(camera.id)}
                               onChange={() => toggleChecked(camera.id)} />
                      </td>
                    ) : null}
                    <td>
                      <button type="button" className="row-select" aria-current={selected ? "true" : undefined}
                              onClick={() => void guarded(() => select(camera.id))}>{camera.name}</button>
                      {camera.archived_at ? <span className="tag">Archived</span> : null}
                    </td>
                    <td className="mono">{camera.camera_ip ?? <span className="muted">Direct URL</span>}</td>
                    <td>{camera.group_name ?? <span className="muted">None</span>}</td>
                    <td><span className="status-dot" data-tone={CONNECTION_TONE[connection]} aria-hidden="true" /> {CONNECTION_LABEL[connection]}</td>
                    <td title={camera.recording_health.detail}>
                      <span className="status-dot" data-tone={HEALTH_TONE[camera.recording_health.state]} aria-hidden="true" /> {HEALTH_LABEL[camera.recording_health.state]}
                    </td>
                    <td>{MODE_LABEL[modeOf(camera)]}{camera.recording_enabled ? "" : <span className="muted"> · off</span>}</td>
                    {showBrand ? <td>{BRAND_LABEL[camera.camera_brand ?? "auto"]}</td> : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : null}
      </div>
    </section>
  );
}
