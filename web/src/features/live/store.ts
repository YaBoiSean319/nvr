import { useMemo } from "react";
import { create } from "zustand";
import * as arrange from "./arrangement";
import type { Arrangement, ManualSize } from "./arrangement";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";

export type Quality = "auto" | "main" | "sub";

export interface LoadedView {
  name: string;
  scope: "personal" | "shared";
}

/** An alarm that pops into Live: its camera first, then that camera's neighbours. */
export interface PopIn {
  id: string;
  rule_name: string;
  camera_id: string;
  camera_name: string;
  label: string;
  occurred_at: string;
  camera_ids: string[];
}

type Workspace = Pick<LiveState, "arrangement" | "view" | "baseline" | "activeId" | "enlargedId">;

/** A temporary layout for an alarm, over the person's own workspace, which Return puts back exactly. */
export interface AlarmLayout {
  current: PopIn;
  /** Alarms that arrived while this one is shown wait their turn instead of taking the screen. */
  queue: PopIn[];
  saved: Workspace;
}

interface LiveState {
  arrangement: Arrangement;
  /** The saved view this arrangement came from, and its arrangement when loaded or saved. */
  view: LoadedView | null;
  baseline: Arrangement | null;
  activeId: string | null;
  enlargedId: string | null;
  sidebarOpen: boolean;
  quality: Record<string, Quality>;
  notice: string | null;
  /** The one camera that may be audible; audio always starts muted and is never restored. */
  listeningId: string | null;
  /** Set once the server-side workspace has been applied, so saving starts after restoring. */
  restored: boolean;
  alarmLayout: AlarmLayout | null;

  toggle: (id: string) => void;
  remove: (id: string) => void;
  replace: (target: string, incoming: string) => void;
  place: (slot: number, id: string) => void;
  move: (id: string, direction: -1 | 1) => void;
  setLayout: (size: ManualSize | "auto") => void;
  load: (arrangement: Arrangement, view: LoadedView | null) => void;
  markSaved: (view: LoadedView) => void;
  restore: (arrangement: Arrangement, sidebarOpen: boolean) => void;
  setActive: (id: string) => void;
  enlarge: (id: string) => void;
  unenlarge: () => void;
  setQuality: (id: string, quality: Quality) => void;
  setSidebarOpen: (open: boolean) => void;
  setListening: (id: string | null) => void;
  clearNotice: () => void;
  popIn: (alarm: PopIn) => void;
  nextAlarm: () => void;
  returnToMyView: () => void;
  reset: () => void;
}

export function alarmArrangement(alarm: PopIn): Arrangement {
  return { mode: "auto", order: [...new Set(alarm.camera_ids)].slice(0, MAX_DISPLAYED_CAMERAS), slots: [] };
}

function showing(alarm: PopIn) {
  return { arrangement: alarmArrangement(alarm), view: null, baseline: null, activeId: alarm.camera_id, enlargedId: null, listeningId: null, notice: null };
}

const initial = {
  arrangement: arrange.EMPTY,
  view: null,
  baseline: null,
  activeId: null,
  enlargedId: null,
  sidebarOpen: true,
  quality: {},
  notice: null,
  listeningId: null,
  restored: false,
  alarmLayout: null,
};

export const useLiveStore = create<LiveState>((set, get) => ({
  ...initial,
  toggle: id => {
    if (arrange.members(get().arrangement).includes(id)) {
      get().remove(id);
      return;
    }
    const { arrangement, notice } = arrange.add(get().arrangement, id);
    set({ arrangement, notice: notice ?? null, activeId: arrange.members(arrangement).includes(id) ? id : get().activeId });
  },
  remove: id =>
    set(state => {
      const before = arrange.members(state.arrangement);
      const arrangement = arrange.remove(state.arrangement, id);
      const after = arrange.members(arrangement);
      const index = before.indexOf(id);
      // Removing the active tile hands control to its neighbour.
      const activeId = state.activeId === id ? (after[Math.min(index, after.length - 1)] ?? null) : state.activeId;
      return {
        arrangement, activeId, notice: null,
        enlargedId: state.enlargedId === id ? null : state.enlargedId,
        listeningId: state.listeningId === id ? null : state.listeningId,
      };
    }),
  replace: (target, incoming) =>
    set(state => ({
      arrangement: arrange.replace(state.arrangement, target, incoming),
      activeId: incoming,
      listeningId: state.listeningId === target ? null : state.listeningId,
    })),
  place: (slot, id) => set(state => ({ arrangement: arrange.place(state.arrangement, slot, id), activeId: id })),
  move: (id, direction) => set(state => ({ arrangement: arrange.move(state.arrangement, id, direction) })),
  setLayout: size => {
    if (size === "auto") {
      set(state => ({ arrangement: arrange.toAuto(state.arrangement), notice: null, enlargedId: null }));
      return;
    }
    const { arrangement, notice } = arrange.toManual(get().arrangement, size);
    set({ arrangement, notice: notice ?? null, enlargedId: null });
  },
  // Choosing a view is the person taking the screen back: any alarm layout ends.
  load: (arrangement, view) =>
    set({ arrangement, view, alarmLayout: null, baseline: view ? arrangement : null, enlargedId: null, listeningId: null, notice: null,
          activeId: arrange.members(arrangement)[0] ?? null }),
  markSaved: view => set(state => ({ view, baseline: state.arrangement })),
  restore: (arrangement, sidebarOpen) =>
    set({ arrangement, sidebarOpen, restored: true, activeId: arrange.members(arrangement)[0] ?? null }),
  setActive: id => set({ activeId: id }),
  enlarge: id => set({ enlargedId: id, activeId: id }),
  unenlarge: () => set({ enlargedId: null }),
  setQuality: (id, quality) => set(state => ({ quality: { ...state.quality, [id]: quality } })),
  setSidebarOpen: open => set({ sidebarOpen: open }),
  setListening: id => set({ listeningId: id }),
  clearNotice: () => set({ notice: null }),
  popIn: alarm => {
    const layout = get().alarmLayout;
    if (layout) {
      if (layout.current.id === alarm.id || layout.queue.some(waiting => waiting.id === alarm.id)) return;
      set({ alarmLayout: { ...layout, queue: [...layout.queue, alarm] } });
      return;
    }
    const { arrangement, view, baseline, activeId, enlargedId } = get();
    set({ ...showing(alarm), alarmLayout: { current: alarm, queue: [], saved: { arrangement, view, baseline, activeId, enlargedId } } });
  },
  nextAlarm: () => {
    const layout = get().alarmLayout;
    const [next, ...rest] = layout?.queue ?? [];
    if (!layout || !next) return;
    set({ ...showing(next), alarmLayout: { ...layout, current: next, queue: rest } });
  },
  returnToMyView: () => {
    const layout = get().alarmLayout;
    if (!layout) return;
    set({ ...layout.saved, alarmLayout: null, listeningId: null, notice: null });
  },
  reset: () => set(initial),
}));

export function useCameraIds(): string[] {
  const arrangement = useLiveStore(state => state.arrangement);
  return useMemo(() => arrange.members(arrangement), [arrangement]);
}

export function useDirty(): boolean {
  return useLiveStore(state => state.view !== null && state.baseline !== null && !arrange.sameArrangement(state.arrangement, state.baseline));
}
