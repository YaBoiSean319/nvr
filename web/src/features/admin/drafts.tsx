import { useEffect, useRef } from "react";
import { useBlocker } from "react-router";
import { create } from "zustand";
import { Modal } from "@/components/Modal";

/**
 * The one unsaved draft on screen (an editor tab), and the Save / Discard / Cancel decision
 * before anything replaces it: another camera, another tab, another workspace or sign-out.
 */
export interface DraftOwner {
  /** What is unsaved, for the dialog: "General & Connection for Lobby". */
  label: string;
  /** Resolves true only when everything was saved. */
  save: () => Promise<boolean>;
  discard: () => void;
}

interface DraftState {
  owner: DraftOwner | null;
  asking: { resolve: (proceed: boolean) => void } | null;
  saving: boolean;
  setOwner: (owner: DraftOwner | null) => void;
}

export const useDraftStore = create<DraftState>(set => ({
  owner: null,
  asking: null,
  saving: false,
  setOwner: owner => set({ owner }),
}));

/** Register a dirty draft while `dirty`; the latest callbacks are always used. */
export function useDraft(dirty: boolean, owner: DraftOwner) {
  const latest = useRef(owner);
  latest.current = owner;
  useEffect(() => {
    if (!dirty) return;
    const registered: DraftOwner = {
      get label() { return latest.current.label; },
      save: () => latest.current.save(),
      discard: () => latest.current.discard(),
    };
    useDraftStore.getState().setOwner(registered);
    return () => {
      if (useDraftStore.getState().owner === registered) useDraftStore.getState().setOwner(null);
    };
  }, [dirty]);
}

/** Ask before replacing an unsaved draft. Resolves true when it is fine to continue. */
export function confirmLeave(): Promise<boolean> {
  const { owner, asking } = useDraftStore.getState();
  if (!owner) return Promise.resolve(true);
  if (asking) return Promise.resolve(false);
  return new Promise(resolve => useDraftStore.setState({ asking: { resolve } }));
}

/** Run `action` only after any unsaved draft was saved or discarded. */
export async function guarded(action: () => void): Promise<void> {
  if (await confirmLeave()) action();
}

/** Renders the decision dialog, blocks in-app navigation and warns on browser close. */
export function DraftGuard() {
  const owner = useDraftStore(state => state.owner);
  const asking = useDraftStore(state => state.asking);
  const saving = useDraftStore(state => state.saving);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => Boolean(useDraftStore.getState().owner) && currentLocation.pathname !== nextLocation.pathname);

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    void confirmLeave().then(proceed => (proceed ? blocker.proceed() : blocker.reset()));
  }, [blocker]);

  useEffect(() => {
    if (!owner) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [owner]);

  function finish(proceed: boolean) {
    asking?.resolve(proceed);
    useDraftStore.setState({ asking: null, saving: false });
  }

  async function save() {
    if (!owner) return finish(true);
    useDraftStore.setState({ saving: true });
    const saved = await owner.save().catch(() => false);
    // A failed save keeps the draft and the current target on screen.
    finish(saved);
  }

  return (
    <Modal open={Boolean(asking)} onOpenChange={open => !open && !saving && finish(false)} title="Unsaved changes"
           description={owner ? `${owner.label} has changes that are not saved.` : undefined}
           footer={
             <>
               <button type="button" className="button" onClick={() => finish(false)} disabled={saving}>Cancel</button>
               <button type="button" className="button" data-variant="danger" disabled={saving}
                       onClick={() => { owner?.discard(); useDraftStore.getState().setOwner(null); finish(true); }}>Discard</button>
               <button type="button" className="button" data-variant="primary" disabled={saving} onClick={() => void save()}>
                 {saving ? "Saving…" : "Save"}
               </button>
             </>
           }>
      <p className="muted">Save keeps your changes and continues. Discard throws them away. Cancel stays here with your edits.</p>
    </Modal>
  );
}
