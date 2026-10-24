import { useEffect } from "react";
import { fetchLiveWorkspace, saveLiveWorkspace } from "@/api/queries";
import type { Camera } from "@/api/types";
import { fromSaved, toSaved } from "./arrangement";
import { useLiveStore } from "./store";

const SAVE_DELAY_MS = 600;

/**
 * Restore the signed-in user's Live workspace once cameras are known, then keep
 * it saved on the server. Audio is never restored.
 */
export function useLiveWorkspacePersistence(cameras: Camera[] | undefined) {
  const restored = useLiveStore(store => store.restored);
  const arrangement = useLiveStore(store => store.arrangement);
  const sidebarOpen = useLiveStore(store => store.sidebarOpen);
  // An alarm layout is temporary: the person's own workspace stays saved as it was.
  const alarmShown = useLiveStore(store => store.alarmLayout !== null);

  useEffect(() => {
    if (restored || !cameras) return;
    const controller = new AbortController();
    const available = new Set(cameras.filter(camera => !camera.archived_at).map(camera => camera.id));
    fetchLiveWorkspace(controller.signal)
      .then(saved => {
        const arrangement = saved.camera_ids === null ? { mode: "auto" as const, order: [], slots: [] } : fromSaved({ ...saved, camera_ids: saved.camera_ids }, available);
        useLiveStore.getState().restore(arrangement, !saved.sidebar_collapsed);
      })
      .catch(() => {
        if (!controller.signal.aborted) useLiveStore.getState().restore(useLiveStore.getState().arrangement, true);
      });
    return () => controller.abort();
  }, [cameras, restored]);

  useEffect(() => {
    if (!restored || alarmShown) return;
    const timer = window.setTimeout(() => {
      const layout = toSaved(arrangement);
      void saveLiveWorkspace({ ...layout, sidebar_collapsed: !sidebarOpen }).catch(() => undefined);
    }, SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [arrangement, sidebarOpen, restored, alarmShown]);
}
