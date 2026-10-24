import * as Menu from "@radix-ui/react-dropdown-menu";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { useDeleteView, useLiveViews, useSaveView } from "@/api/queries";
import type { Role } from "@/api/types";
import { toSaved, type ManualSize } from "./arrangement";
import { SaveAsDialog } from "./dialogs";
import { useCameraIds, useDirty, useLiveStore } from "./store";

const LAYOUTS: { label: string; size: ManualSize }[] = [
  { label: "1 × 1", size: 1 },
  { label: "2 × 2", size: 4 },
  { label: "3 × 3", size: 9 },
  { label: "4 × 4", size: 16 },
];

export function ViewBar({ role }: { role: Role }) {
  const arrangement = useLiveStore(store => store.arrangement);
  const view = useLiveStore(store => store.view);
  const { markSaved, setLayout, load } = useLiveStore.getState();
  const dirty = useDirty();
  const cameraIds = useCameraIds();
  const views = useLiveViews();
  const save = useSaveView();
  const remove = useDeleteView();
  const [saveAs, setSaveAs] = useState(false);
  const canSave = view !== null && (view.scope === "personal" || role === "administrator");
  const error = save.error ?? remove.error;

  return (
    <div className="view-bar">
      <span className="view-name" data-dirty={dirty}>
        {view ? (
          <>
            {view.name} <span className="muted">· {view.scope === "shared" ? "Shared view" : "My view"}</span>
            {dirty ? <span className="view-dirty" title="Changed since loaded"> · Unsaved changes</span> : null}
          </>
        ) : (
          <span className="muted">Unsaved arrangement</span>
        )}
      </span>
      {canSave && dirty ? (
        <button type="button" className="button" disabled={save.isPending}
                onClick={() => save.mutate({ name: view.name, scope: view.scope, layout: toSaved(arrangement) }, { onSuccess: () => markSaved(view) })}>
          Save
        </button>
      ) : null}
      <button type="button" className="button" disabled={!cameraIds.length} onClick={() => setSaveAs(true)}>Save as…</button>
      <Menu.Root>
        <Menu.Trigger className="button" aria-label="Layout">Layout: {arrangement.mode === "auto" ? "Auto" : `${Math.sqrt(arrangement.slots.length)} × ${Math.sqrt(arrangement.slots.length)}`}</Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className="menu" align="end" sideOffset={4}>
            <Menu.RadioGroup value={arrangement.mode === "auto" ? "auto" : String(arrangement.slots.length)}
                             onValueChange={value => setLayout(value === "auto" ? "auto" : (Number(value) as ManualSize))}>
              <Menu.RadioItem className="menu-item" value="auto"><Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>Auto</Menu.RadioItem>
              <Menu.Separator className="menu-separator" />
              <Menu.Label className="menu-label">Manual layout</Menu.Label>
              {LAYOUTS.map(layout => (
                <Menu.RadioItem key={layout.size} className="menu-item" value={String(layout.size)}>
                  <Menu.ItemIndicator className="menu-indicator">✓</Menu.ItemIndicator>{layout.label}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
      {canSave ? (
        <button type="button" className="button" data-variant="danger" disabled={remove.isPending}
                onClick={() => {
                  if (window.confirm(`Delete the view “${view.name}”? Cameras stay on screen.`)) {
                    remove.mutate(view, { onSuccess: () => load(arrangement, null) });
                  }
                }}>
          Delete view
        </button>
      ) : null}
      {error ? <span className="view-error" role="alert">{error instanceof ApiError ? error.detail : "The view could not be changed."}</span> : null}
      <SaveAsDialog open={saveAs} onOpenChange={setSaveAs} arrangement={arrangement} role={role} existing={views.data ?? []}
                    onSaved={saved => markSaved(saved)} />
    </div>
  );
}
