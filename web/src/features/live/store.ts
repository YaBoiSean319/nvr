import { useMemo } from "react";
import { create } from "zustand";
import * as arrange from "./arrangement";
import type { Arrangement, ManualSize } from "./arrangement";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import type { DewarpState } from "@/features/dewarp/geometry";

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
  /** How each fisheye camera is shown (raw when absent), kept with the camera and saved with views and the workspace. */
  dewarp: Record<string, DewarpState>;
  /** The dewarp state of the loaded view when it was loaded or saved, to tell when it has changed. */
  baselineDewarp: Record<string, DewarpState> | null;
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
  load: (arrangement: Arrangement, view: LoadedView | null, dewarp?: Record<string, DewarpState>) => void;
  setDewarp: (id: string, state: DewarpState) => void;
  markSaved: (view: LoadedView) => void;
  restore: (arrangement: Arrangement, sidebarOpen: boolean, dewarp?: Record<string, DewarpState>) => void;
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
  dewarp: {},
  baselineDewarp: null,
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
  load: (arrangement, view, dewarp) =>
    set(state => {
      const shown = dewarp ?? (view ? {} : state.dewarp); // A saved view brings its own dewarping; an ad-hoc selection keeps how each camera was shown
      return { arrangement, view, alarmLayout: null, baseline: view ? arrangement : null, dewarp: shown, baselineDewarp: view ? shown : null, enlargedId: null, listeningId: null, notice: null,
               activeId: arrange.members(arrangement)[0] ?? null };
    }),
  setDewarp: (id, next) => set(state => ({ dewarp: next.mode === "raw" ? Object.fromEntries(Object.entries(state.dewarp).filter(([key]) => key !== id)) : { ...state.dewarp, [id]: next } })),
  markSaved: view => set(state => ({ view, baseline: state.arrangement, baselineDewarp: state.dewarp })),
  restore: (arrangement, sidebarOpen, dewarp) =>
    set({ arrangement, sidebarOpen, restored: true, dewarp: dewarp ?? {}, activeId: arrange.members(arrangement)[0] ?? null }),
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

/** The dewarp states of the cameras in an arrangement that are not raw: what a saved view keeps. */
export function keptDewarp(dewarp: Record<string, DewarpState>, arrangement: Arrangement): Record<string, DewarpState> {
  const ids = new Set(arrange.members(arrangement));
  return Object.fromEntries(Object.entries(dewarp).filter(([id, state]) => ids.has(id) && state.mode !== "raw"));
}

/** JSON with its keys in a fixed order, so two states with the same content compare equal whatever order they were built in. */
function stable(value: Record<string, DewarpState>): string {
  return JSON.stringify(Object.keys(value).sort().map(id => [id, value[id]]));
}

export function useDirty(): boolean {
  return useLiveStore(state => state.view !== null && state.baseline !== null
    && (!arrange.sameArrangement(state.arrangement, state.baseline)
      || stable(keptDewarp(state.dewarp, state.arrangement)) !== stable(keptDewarp(state.baselineDewarp ?? {}, state.baseline))));
}
