import { useMemo, useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { useSaveView } from "@/api/queries";
import type { Camera, Role, SavedView } from "@/api/types";
import { Modal } from "@/components/Modal";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { toSaved, type Arrangement } from "./arrangement";
import type { LoadedView } from "./store";

export function SaveAsDialog({ open, onOpenChange, arrangement, role, existing, onSaved }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  arrangement: Arrangement;
  role: Role;
  existing: SavedView[];
  onSaved: (view: LoadedView) => void;
}) {
  const save = useSaveView();
  const [name, setName] = useState("");
  const [scope, setScope] = useState<SavedView["scope"]>("personal");
  const trimmed = name.trim();
  const clash = existing.some(view => view.scope === scope && view.name.toLowerCase() === trimmed.toLowerCase());

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!trimmed || trimmed.length > 60) return;
    save.mutate({ name: trimmed, scope, layout: toSaved(arrangement) }, {
      onSuccess: () => {
        onSaved({ name: trimmed, scope });
        onOpenChange(false);
        setName("");
      },
    });
  }

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Save view as" description="Saves these cameras, their order and the layout. Search dates are never part of a view."
      footer={<>
        <button type="button" className="button" onClick={() => onOpenChange(false)}>Cancel</button>
        <button type="submit" form="save-view-form" className="button" data-variant="primary" disabled={!trimmed || save.isPending}>
          {clash ? "Replace view" : "Save view"}
        </button>
      </>}>
      <form id="save-view-form" className="auth-form" onSubmit={submit}>
        <div className="field">
          <label htmlFor="view-name">Name</label>
          <input id="view-name" value={name} maxLength={60} onChange={event => setName(event.target.value)} autoFocus />
          {clash ? <span className="hint">A {scope} view with this name exists and will be replaced.</span> : null}
        </div>
        {role === "administrator" ? (
          <fieldset className="field radio-row">
            <legend>Who can use it</legend>
            <label><input type="radio" name="scope" checked={scope === "personal"} onChange={() => setScope("personal")} /> Only me</label>
            <label><input type="radio" name="scope" checked={scope === "shared"} onChange={() => setScope("shared")} /> Everyone (shared)</label>
          </fieldset>
        ) : null}
        {save.error ? <p className="notice" data-tone="bad" role="alert">{save.error instanceof ApiError ? save.error.detail : "The view could not be saved."}</p> : null}
      </form>
    </Modal>
  );
}

/** More cameras than a view can show: choose which ones, never truncate silently. */
export function SubsetChooser({ open, onOpenChange, title, cameras, onApply }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  cameras: Camera[];
  onApply: (ids: string[]) => void;
}) {
  const [chosen, setChosen] = useState<string[]>(() => cameras.slice(0, MAX_DISPLAYED_CAMERAS).map(camera => camera.id));
  const full = chosen.length >= MAX_DISPLAYED_CAMERAS;
  function flip(id: string) {
    setChosen(current => (current.includes(id) ? current.filter(existing => existing !== id) : full ? current : [...current, id]));
  }
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={`Choose cameras from ${title}`}
      description={`${title} has ${cameras.length} cameras; a view shows up to ${MAX_DISPLAYED_CAMERAS}. Choose which to show.`}
      footer={<>
        <span className="muted modal-count" aria-live="polite">{chosen.length}/{MAX_DISPLAYED_CAMERAS} chosen</span>
        <button type="button" className="button" onClick={() => onOpenChange(false)}>Cancel</button>
        <button type="button" className="button" data-variant="primary" disabled={!chosen.length} onClick={() => { onApply(chosen); onOpenChange(false); }}>
          Show {chosen.length} camera{chosen.length === 1 ? "" : "s"}
        </button>
      </>}>
      <ul className="choice-list">
        {cameras.map(camera => (
          <li key={camera.id}>
            <label>
              <input type="checkbox" checked={chosen.includes(camera.id)} disabled={!chosen.includes(camera.id) && full} onChange={() => flip(camera.id)} />
              {camera.name}
            </label>
          </li>
        ))}
      </ul>
    </Modal>
  );
}

/** Keyboard and touch alternative to dragging a camera onto a tile. */
export function ReplaceChooser({ open, onOpenChange, target, cameras, inView, onChoose }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  target: Camera | undefined;
  cameras: Camera[];
  inView: string[];
  onChoose: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const options = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return cameras
      .filter(camera => camera.id !== target?.id && !camera.archived_at && (!needle || camera.name.toLowerCase().includes(needle)))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  }, [cameras, query, target]);
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={`Replace ${target?.name ?? "camera"}`}
      description="Choose the camera for this position. A camera already in the view swaps places."
      footer={<button type="button" className="button" onClick={() => onOpenChange(false)}>Cancel</button>}>
      <input className="browser-search" type="search" aria-label="Search cameras" placeholder="Search cameras" value={query} onChange={event => setQuery(event.target.value)} autoFocus />
      <ul className="choice-list">
        {options.map(camera => (
          <li key={camera.id}>
            <button type="button" className="browser-row" onClick={() => { onChoose(camera.id); onOpenChange(false); }}>
              <span />
              <span />
              <span className="browser-name">{camera.name}</span>
              {inView.includes(camera.id) ? <span className="browser-ip">swap</span> : null}
            </button>
          </li>
        ))}
      </ul>
    </Modal>
  );
}
