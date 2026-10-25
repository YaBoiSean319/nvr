import * as Tabs from "@radix-ui/react-tabs";
import { useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { keys } from "@/api/queries";
import type { Camera } from "@/api/types";
import { Modal } from "@/components/Modal";
import { HEALTH_TONE } from "@/features/cameras/CameraBrowser";
import { guarded } from "../drafts";
import { archiveCamera, restoreCamera } from "./api";
import { AiTab } from "./AiTab";
import { GeneralTab } from "./GeneralTab";
import { HealthTab } from "./HealthTab";
import { MotionTab } from "./MotionTab";
import { NeighboursTab } from "./NeighboursTab";
import { PtzTab } from "./PtzTab";
import { RecordingTab } from "./RecordingTab";
import { ScheduleTab } from "./ScheduleTab";
import { CONNECTION_LABEL, HEALTH_LABEL, MODE_LABEL, connectionOf, modeOf } from "./inventory";
import { Preview } from "./Preview";
import { useCamerasStore, type EditorTab } from "./store";

export const TAB_LABEL: Record<EditorTab, string> = {
  general: "General & Connection",
  recording: "Recording",
  motion: "Motion",
  ai: "AI",
  ptz: "PTZ",
  neighbours: "Neighbours",
  schedule: "Schedule & Retention",
  health: "Health & History",
};

/** Tabs not yet rebuilt; filled in by later slices. */
export type TabContent = Partial<Record<EditorTab, (camera: Camera, admin: boolean) => ReactNode>>;

const CONTENT: TabContent = {
  general: (camera, admin) => <GeneralTab key={camera.id} camera={camera} admin={admin} />,
  recording: (camera, admin) => <RecordingTab key={camera.id} camera={camera} admin={admin} />,
  motion: (camera, admin) => <MotionTab key={camera.id} camera={camera} admin={admin} />,
  ai: (camera, admin) => <AiTab key={camera.id} camera={camera} admin={admin} />,
  ptz: (camera, admin) => <PtzTab key={camera.id} camera={camera} admin={admin} />,
  neighbours: (camera, admin) => <NeighboursTab key={camera.id} camera={camera} admin={admin} />,
  schedule: (camera, admin) => <ScheduleTab key={camera.id} camera={camera} admin={admin} />,
  health: camera => <HealthTab key={camera.id} camera={camera} />,
};

/** Exactly one camera's settings, with a fixed identity header and one tab at a time. */
export function CameraEditor({ camera, admin, extraTabs = {} }: { camera: Camera; admin: boolean; extraTabs?: TabContent }) {
  const client = useQueryClient();
  const tab = useCamerasStore(state => state.tab);
  const previewing = useCamerasStore(state => state.previewing);
  const { setTab, setPreviewing } = useCamerasStore.getState();
  const [archiving, setArchiving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const content = { ...CONTENT, ...extraTabs };
  const archived = Boolean(camera.archived_at);
  const connection = connectionOf(camera);

  async function archive() {
    setBusy(true);
    setError(null);
    setPreviewing(false);
    try {
      await archiveCamera(camera.id);
      setArchiving(false);
      await client.invalidateQueries({ queryKey: keys.cameras });
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "The camera could not be archived.");
    } finally {
      setBusy(false);
    }
  }

  async function restore() {
    setBusy(true);
    setError(null);
    try {
      await restoreCamera(camera.id);
      await client.invalidateQueries({ queryKey: keys.cameras });
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "The camera could not be restored.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="editor" aria-label={`Settings for ${camera.name}`}>
      <header className="editor-head">
        <div className="editor-identity">
          <h2>{camera.name}{archived ? <span className="tag">Archived</span> : null}</h2>
          <p className="muted">
            <span className="mono">{camera.camera_ip ?? camera.stream_url}</span>
            {" · "}{CONNECTION_LABEL[connection]}
            {" · "}<span className="status-dot" data-tone={HEALTH_TONE[camera.recording_health.state]} aria-hidden="true" /> {HEALTH_LABEL[camera.recording_health.state]}
            {" · "}{MODE_LABEL[modeOf(camera)]}{camera.recording_enabled ? "" : " (recording off)"}
          </p>
        </div>
        <div className="editor-actions">
          {!archived && !previewing ? <button type="button" className="button" onClick={() => setPreviewing(true)}>Show preview</button> : null}
          {admin && !archived ? <button type="button" className="button" data-variant="danger" onClick={() => setArchiving(true)}>Archive…</button> : null}
          {admin && archived ? <button type="button" className="button" onClick={() => void restore()} disabled={busy}>Restore camera</button> : null}
        </div>
      </header>
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      {previewing && !archived ? <Preview camera={camera} onClose={() => setPreviewing(false)} /> : null}
      <Tabs.Root className="editor-tabs" value={tab} onValueChange={value => void guarded(() => setTab(value as EditorTab))}>
        <Tabs.List className="tab-list editor-tab-list" aria-label="Camera settings">
          {(Object.keys(TAB_LABEL) as EditorTab[]).map(key => <Tabs.Trigger key={key} value={key} className="tab">{TAB_LABEL[key]}</Tabs.Trigger>)}
        </Tabs.List>
        {(Object.keys(TAB_LABEL) as EditorTab[]).map(key => (
          <Tabs.Content key={key} value={key} className="editor-panel">
            {tab === key
              ? content[key]?.(camera, admin) ?? (
                <p className="muted">These settings are being rebuilt in this interface. Use the current interface to change them for now.</p>
              )
              : null}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      <Modal open={archiving} onOpenChange={open => !busy && setArchiving(open)} title={`Archive ${camera.name}?`}
             description="Archiving stops recording and live video for this camera. Its recorded footage, holds and exports are kept, and you can restore the camera later."
             footer={
               <>
                 <button type="button" className="button" onClick={() => setArchiving(false)} disabled={busy}>Cancel</button>
                 <button type="button" className="button" data-variant="danger" onClick={() => void archive()} disabled={busy}>{busy ? "Archiving…" : "Archive camera"}</button>
               </>
             }>
        <p className="muted">Archived cameras stay in the inventory under “Archived cameras”.</p>
      </Modal>
    </section>
  );
}
