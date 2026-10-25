import { useMemo, useState } from "react";
import { ApiError } from "@/api/client";
import { useCameras, useMe, usePreferences, useSavePreferences } from "@/api/queries";
import { AlarmBanner } from "./AlarmBanner";
import { LiveBrowser } from "./LiveBrowser";
import { LiveGrid } from "./LiveGrid";
import { ReplaceChooser } from "./dialogs";
import { useLiveWorkspacePersistence } from "./persistence";
import { useAlarmPopIns } from "./popIns";
import { useCameraIds, useLiveStore } from "./store";
import { ViewBar } from "./ViewBar";

export function LiveWorkspace() {
  const cameras = useCameras();
  const me = useMe();
  const cameraIds = useCameraIds();
  const sidebarOpen = useLiveStore(store => store.sidebarOpen);
  const restored = useLiveStore(store => store.restored);
  const notice = useLiveStore(store => store.notice);
  const { setSidebarOpen, clearNotice, replace } = useLiveStore.getState();
  const [replacing, setReplacing] = useState<string | null>(null);
  const byId = useMemo(() => new Map((cameras.data ?? []).filter(camera => !camera.archived_at).map(camera => [camera.id, camera])), [cameras.data]);
  const error = cameras.error instanceof ApiError ? cameras.error.detail : cameras.error ? "Cameras could not be loaded." : null;
  useLiveWorkspacePersistence(cameras.data);
  const preferences = usePreferences(true);
  const savePreferences = useSavePreferences();
  const followAlarms = Boolean(preferences.data?.live_alarm_pop_in);
  useAlarmPopIns(followAlarms && restored, useLiveStore.getState().popIn);

  return (
    <div className="workspace" data-sidebar={sidebarOpen ? "open" : "closed"}>
      {sidebarOpen ? <LiveBrowser cameras={cameras.data} loading={cameras.isPending} error={error} /> : null}
      <section className="canvas" aria-label="Live video">
        <div className="canvas-bar">
          {!sidebarOpen ? <button type="button" className="button" onClick={() => setSidebarOpen(true)} aria-label="Show camera browser">Cameras</button> : null}
          <span className="canvas-title">Live</span>
          <span className="muted canvas-count">{cameraIds.length ? `${cameraIds.length} camera${cameraIds.length === 1 ? "" : "s"}` : ""}</span>
          {notice ? (
            <p className="canvas-notice" role="status">
              {notice}
              <button type="button" className="button" data-variant="ghost" onClick={clearNotice} aria-label="Dismiss">✕</button>
            </p>
          ) : null}
          <button type="button" className="button" aria-pressed={followAlarms} disabled={!preferences.data || savePreferences.isPending}
                  title="Bring cameras with new alarms into Live, with their neighbours"
                  onClick={() => savePreferences.mutate({ live_alarm_pop_in: !followAlarms })}>
            Alarm pop-in {followAlarms ? "on" : "off"}
          </button>
          {me.data ? <ViewBar role={me.data.role} /> : null}
        </div>
        <AlarmBanner />
        {restored ? <LiveGrid cameras={byId} onReplaceRequest={setReplacing} /> : <div className="grid-empty"><span className="spinner" aria-label="Restoring your workspace" /></div>}
      </section>
      <ReplaceChooser open={replacing !== null} onOpenChange={open => !open && setReplacing(null)} target={replacing ? byId.get(replacing) : undefined}
                      cameras={cameras.data ?? []} inView={cameraIds} onChoose={id => replacing && replace(replacing, id)} />
    </div>
  );
}
