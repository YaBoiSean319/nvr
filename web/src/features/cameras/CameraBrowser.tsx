import { useDeferredValue, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import type { Camera } from "@/api/types";

export const HEALTH_TONE: Record<Camera["recording_health"]["state"], "good" | "warn" | "bad" | undefined> = {
  recording: "good",
  expected_idle: undefined,
  uncertain: "warn",
  motion_source_degraded: "warn",
  camera_unreachable: "bad",
  write_stalled: "bad",
};

export const CAMERA_DRAG_TYPE = "application/x-nvr-camera";

interface CameraListProps {
  cameras: Camera[] | undefined;
  loading: boolean;
  error: string | null;
  selected: string[];
  onToggle: (id: string) => void;
  /** Shift+Enter on a row: replace the active tile (keyboard alternative to dragging). */
  onReplaceActive?: (id: string) => void;
  capacity: number;
}

/** Alphabetized, searchable cameras; one click adds or removes a camera from the current view. */
export function CameraList({ cameras, loading, error, selected, onToggle, onReplaceActive, capacity }: CameraListProps) {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim().toLowerCase());
  const chosen = useMemo(() => new Set(selected), [selected]);
  const visible = useMemo(() => {
    const active = (cameras ?? []).filter(camera => !camera.archived_at);
    const matches = deferred
      ? active.filter(camera => camera.name.toLowerCase().includes(deferred) || (camera.camera_ip ?? "").includes(deferred))
      : active;
    return [...matches].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }) || a.id.localeCompare(b.id));
  }, [cameras, deferred]);

  function onRowKey(event: KeyboardEvent<HTMLButtonElement>, id: string) {
    if (event.key === "Enter" && event.shiftKey && onReplaceActive) {
      event.preventDefault();
      onReplaceActive(id);
    }
  }

  return (
    <div className="browser-section">
      <input className="browser-search" type="search" placeholder="Search name or address" aria-label="Search cameras by name or address"
             value={query} onChange={event => setQuery(event.target.value)} />
      <div className="browser-meta" aria-live="polite">
        {selected.length}/{capacity} in view{deferred ? ` · ${visible.length} match${visible.length === 1 ? "" : "es"}` : ""}
      </div>
      <div className="browser-list">
        {loading ? <p className="muted browser-empty">Loading cameras…</p> : null}
        {error ? <p className="notice browser-empty" data-tone="bad" role="alert">{error}</p> : null}
        {!loading && !error && !visible.length ? <p className="muted browser-empty">{deferred ? "No cameras match." : "No cameras have been added yet."}</p> : null}
        <ul role="list">
          {visible.map(camera => {
            const isSelected = chosen.has(camera.id);
            const tone = HEALTH_TONE[camera.recording_health.state];
            return (
              <li key={camera.id}>
                <button type="button" className="browser-row" aria-pressed={isSelected} onClick={() => onToggle(camera.id)}
                        onKeyDown={event => onRowKey(event, camera.id)} title={camera.recording_health.detail}
                        draggable onDragStart={event => event.dataTransfer.setData(CAMERA_DRAG_TYPE, camera.id)}>
                  <span className="browser-check" aria-hidden="true">{isSelected ? "✓" : ""}</span>
                  <span className="status-dot" data-tone={tone} aria-hidden="true" />
                  <span className="browser-name">{camera.name}</span>
                  {camera.camera_ip ? <span className="browser-ip">{camera.camera_ip}</span> : null}
                  <span className="visually-hidden">{camera.recording_health.detail}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

/** The collapsible browser frame shared by workspaces. */
export function BrowserFrame({ onCollapse, children }: { onCollapse: () => void; children: ReactNode }) {
  return (
    <aside className="browser" aria-label="Camera browser">
      <button type="button" className="button icon-button browser-collapse" data-variant="ghost" onClick={onCollapse}
              aria-label="Hide camera browser" title="Hide camera browser">⟨</button>
      {children}
    </aside>
  );
}
