import { useMemo } from "react";
import { ApiError } from "@/api/client";
import { useCameras } from "@/api/queries";
import { CameraBrowser } from "@/features/cameras/CameraBrowser";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { LiveGrid } from "./LiveGrid";
import { useLiveStore } from "./store";

export function LiveWorkspace() {
  const cameras = useCameras();
  const cameraIds = useLiveStore(store => store.cameraIds);
  const sidebarOpen = useLiveStore(store => store.sidebarOpen);
  const notice = useLiveStore(store => store.notice);
  const { toggle, setSidebarOpen, clearNotice } = useLiveStore.getState();
  const byId = useMemo(() => new Map((cameras.data ?? []).filter(camera => !camera.archived_at).map(camera => [camera.id, camera])), [cameras.data]);
  const error = cameras.error instanceof ApiError ? cameras.error.detail : cameras.error ? "Cameras could not be loaded." : null;

  return (
    <div className="workspace" data-sidebar={sidebarOpen ? "open" : "closed"}>
      {sidebarOpen ? (
        <CameraBrowser cameras={cameras.data} loading={cameras.isPending} error={error} selected={cameraIds}
                       onToggle={toggle} onCollapse={() => setSidebarOpen(false)} capacity={MAX_DISPLAYED_CAMERAS} />
      ) : null}
      <section className="canvas" aria-label="Live video">
        <div className="canvas-bar">
          {!sidebarOpen ? (
            <button type="button" className="button" onClick={() => setSidebarOpen(true)} aria-label="Show camera browser">Cameras</button>
          ) : null}
          <span className="canvas-title">Live</span>
          <span className="muted canvas-count">{cameraIds.length ? `${cameraIds.length} camera${cameraIds.length === 1 ? "" : "s"}` : ""}</span>
          {notice ? (
            <p className="canvas-notice" role="status">
              {notice}
              <button type="button" className="button" data-variant="ghost" onClick={clearNotice} aria-label="Dismiss">✕</button>
            </p>
          ) : null}
        </div>
        <LiveGrid cameras={byId} />
      </section>
    </div>
  );
}
