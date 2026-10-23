import * as Tabs from "@radix-ui/react-tabs";
import { useState } from "react";
import { useCameraGroups, useLiveViews } from "@/api/queries";
import type { Camera, CameraGroup, SavedView } from "@/api/types";
import { BrowserFrame, CameraList } from "@/features/cameras/CameraBrowser";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { fromSaved } from "./arrangement";
import { SubsetChooser } from "./dialogs";
import { useCameraIds, useLiveStore } from "./store";

export function LiveBrowser({ cameras, loading, error }: { cameras: Camera[] | undefined; loading: boolean; error: string | null }) {
  const selected = useCameraIds();
  const activeId = useLiveStore(store => store.activeId);
  const { toggle, replace, load, setSidebarOpen } = useLiveStore.getState();
  const groups = useCameraGroups();
  const views = useLiveViews();
  const [chooser, setChooser] = useState<{ title: string; cameras: Camera[] } | null>(null);
  // Groups and views name camera IDs; opening one before the cameras are known would drop them all.
  const ready = cameras !== undefined;
  const available = new Map((cameras ?? []).filter(camera => !camera.archived_at).map(camera => [camera.id, camera]));

  function openGroup(group: CameraGroup) {
    const members = [...new Set(group.camera_ids)]
      .map(id => available.get(id))
      .filter((camera): camera is Camera => Boolean(camera))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    if (members.length > MAX_DISPLAYED_CAMERAS) {
      setChooser({ title: group.name, cameras: members });
      return;
    }
    load({ mode: "auto", order: members.map(camera => camera.id), slots: [] }, null);
  }

  function openView(view: SavedView) {
    load(fromSaved(view, new Set(available.keys())), { name: view.name, scope: view.scope });
  }

  return (
    <BrowserFrame onCollapse={() => setSidebarOpen(false)}>
      <Tabs.Root className="browser-tabs" defaultValue="cameras">
        <Tabs.List className="tab-list" aria-label="Browse">
          <Tabs.Trigger className="tab" value="cameras">Cameras</Tabs.Trigger>
          <Tabs.Trigger className="tab" value="groups">Groups</Tabs.Trigger>
          <Tabs.Trigger className="tab" value="views">Views</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content className="tab-panel" value="cameras">
          <CameraList cameras={cameras} loading={loading} error={error} selected={selected} onToggle={toggle}
                      onReplaceActive={id => activeId && replace(activeId, id)} capacity={MAX_DISPLAYED_CAMERAS} />
        </Tabs.Content>
        <Tabs.Content className="tab-panel" value="groups">
          <ul className="browser-list" role="list">
            {(groups.data ?? []).map(group => (
              <li key={group.id}>
                <button type="button" className="browser-row" disabled={!ready} onClick={() => openGroup(group)}>
                  <span />
                  <span />
                  <span className="browser-name">{group.name}</span>
                  <span className="browser-ip">{group.camera_count}</span>
                </button>
              </li>
            ))}
            {groups.data && !groups.data.length ? <li className="muted browser-empty">No camera groups.</li> : null}
          </ul>
        </Tabs.Content>
        <Tabs.Content className="tab-panel" value="views">
          <ul className="browser-list" role="list">
            {(views.data ?? []).map(view => (
              <li key={`${view.scope}:${view.name}`}>
                <button type="button" className="browser-row" disabled={!ready} onClick={() => openView(view)}>
                  <span />
                  <span />
                  <span className="browser-name">{view.name}</span>
                  <span className="browser-ip">{view.scope === "shared" ? "Shared" : "Mine"}</span>
                </button>
              </li>
            ))}
            {views.data && !views.data.length ? <li className="muted browser-empty">No saved views yet. Arrange cameras, then use Save as.</li> : null}
          </ul>
        </Tabs.Content>
      </Tabs.Root>
      {chooser ? (
        <SubsetChooser open onOpenChange={open => !open && setChooser(null)} title={chooser.title} cameras={chooser.cameras}
                       onApply={ids => load({ mode: "auto", order: ids, slots: [] }, null)} />
      ) : null}
    </BrowserFrame>
  );
}
