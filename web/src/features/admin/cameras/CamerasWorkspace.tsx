import { useEffect, useMemo, useRef, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { NavLink } from "react-router";
import { ApiError } from "@/api/client";
import { useCameras, useMe } from "@/api/queries";
import { DraftGuard } from "../drafts";
import { BulkDialog } from "./BulkDialog";
import { CameraEditor, type TabContent } from "./CameraEditor";
import { InventoryTable } from "./InventoryTable";
import { matches, revealing } from "./inventory";
import { useCamerasStore } from "./store";

export type CamerasSection = "inventory" | "add" | "groups";

/** Section navigation shared by Cameras, Add Cameras and Groups & Policies. */
function Sections({ admin }: { admin: boolean }) {
  return (
    <nav className="section-nav" aria-label="Camera administration">
      <NavLink to="/cameras" end className="section-link">Cameras</NavLink>
      {admin ? <NavLink to="/cameras/add" className="section-link">Add Cameras</NavLink> : null}
      {admin ? <NavLink to="/cameras/groups" className="section-link">Groups &amp; Policies</NavLink> : null}
    </nav>
  );
}

export function CamerasShell({ admin, children }: { admin: boolean; children: ReactNode }) {
  return (
    <div className="admin-workspace">
      <Sections admin={admin} />
      {children}
      <DraftGuard />
    </div>
  );
}

/** Inventory beside one selected-camera editor; phones drill into the editor and come back. */
export function CamerasInventory({ extraTabs }: { extraTabs?: TabContent }) {
  const me = useMe();
  const cameras = useCameras();
  const selectedId = useCamerasStore(state => state.selectedId);
  const detailOpen = useCamerasStore(state => state.detailOpen);
  const filters = useCamerasStore(state => state.filters);
  const paneWidth = useCamerasStore(state => state.paneWidth);
  const preferencesFor = useCamerasStore(state => state.preferencesFor);
  const { select, closeDetail, setFilters, setPaneWidth, loadPreferences } = useCamerasStore.getState();
  const admin = me.data?.role === "administrator";
  const panes = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const selected = useMemo(() => cameras.data?.find(camera => camera.id === selectedId) ?? null, [cameras.data, selectedId]);
  const error = cameras.error instanceof ApiError ? cameras.error.detail : cameras.error ? "Cameras could not be loaded." : null;

  // A preview is never restored: coming back to Cameras always starts without one.
  useEffect(() => () => useCamerasStore.getState().setPreviewing(false), []);

  useEffect(() => {
    if (me.data && preferencesFor !== me.data.username) loadPreferences(me.data.username);
  }, [me.data, preferencesFor, loadPreferences]);

  function resizeTo(clientX: number) {
    const rect = panes.current?.getBoundingClientRect();
    if (rect) setPaneWidth(((clientX - rect.left) / rect.width) * 100);
  }

  function onResizerKey(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.key === "ArrowLeft" ? -2 : event.key === "ArrowRight" ? 2 : 0;
    if (event.key === "Home") setPaneWidth(30);
    else if (event.key === "End") setPaneWidth(75);
    else if (step) setPaneWidth(paneWidth + step);
    else return;
    event.preventDefault();
  }

  return (
    <div ref={panes} className="admin-panes" data-detail={selectedId && detailOpen ? "open" : "closed"}
         style={{ gridTemplateColumns: `minmax(0, ${paneWidth}fr) 8px minmax(0, ${100 - paneWidth}fr)` }}>
      <InventoryTable cameras={cameras.data} loading={cameras.isPending} error={error} admin={admin} />
      {admin ? <BulkDialog cameras={cameras.data ?? []} /> : null}
      <div className="pane-resizer" role="separator" aria-orientation="vertical" aria-label="Resize inventory and settings"
           aria-valuemin={30} aria-valuemax={75} aria-valuenow={Math.round(paneWidth)} tabIndex={0} onKeyDown={onResizerKey}
           onPointerDown={(event: PointerEvent<HTMLDivElement>) => { dragging.current = true; event.currentTarget.setPointerCapture(event.pointerId); }}
           onPointerMove={event => dragging.current && resizeTo(event.clientX)}
           onPointerUp={() => { dragging.current = false; }} />
      <div className="editor-pane">
        {selectedId ? (
          <button type="button" className="button back-button" onClick={closeDetail}>← Back to Cameras</button>
        ) : null}
        {!selectedId ? (
          <div className="editor-empty"><p><strong>Select a camera to view settings.</strong></p><p className="muted">Choose a row in the inventory.</p></div>
        ) : !cameras.data ? null : !selected ? (
          <div className="editor-empty" role="status">
            <p><strong>This camera is no longer available.</strong></p>
            <p className="muted">It may have been removed, or you no longer have access to it.</p>
            <button type="button" className="button" onClick={() => select(null)}>Close</button>
          </div>
        ) : (
          <>
            {!matches(selected, filters) ? (
              <p className="notice filtered-notice" data-tone="warn" role="status">
                Selected camera is outside the current filter.
                <button type="button" className="button" onClick={() => setFilters(revealing(selected, filters))}>Show in table</button>
              </p>
            ) : null}
            <CameraEditor key={selected.id} camera={selected} admin={admin} extraTabs={extraTabs} />
          </>
        )}
      </div>
    </div>
  );
}
