import { useSyncExternalStore } from "react";
import { create } from "zustand";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { playbackClock, SPEEDS, type Speed } from "./clock";

/** What the From/To inputs say; `fold` holds the chosen instant (ms) when a local time repeats. */
export interface SearchDraft {
  start: string;
  end: string;
  startFold: string;
  endFold: string;
}

export interface AppliedSearch {
  start: string;
  end: string;
  draft: SearchDraft;
}

export interface TimeWindow {
  start: number;
  end: number;
}

interface PlaybackState {
  cameraIds: string[];
  draft: SearchDraft | null;
  applied: AppliedSearch | null;
  /** Visible part of the applied range (zoom and pan); null shows the whole range. */
  window: TimeWindow | null;
  /** Set when a search is applied: once its timeline arrives, the clock moves to the earliest recording. */
  seekToEarliest: boolean;
  activeId: string | null;
  enlargedId: string | null;
  sidebarOpen: boolean;
  timelineOpen: boolean;
  notice: string | null;
  restoredFor: string | null;

  toggle: (id: string) => void;
  remove: (id: string) => void;
  setDraft: (draft: Partial<SearchDraft>) => void;
  apply: (search: AppliedSearch) => void;
  searchLoaded: () => void;
  setWindow: (window: TimeWindow | null) => void;
  setActive: (id: string) => void;
  enlarge: (id: string) => void;
  unenlarge: () => void;
  setSidebarOpen: (open: boolean) => void;
  setTimelineOpen: (open: boolean) => void;
  clearNotice: () => void;
  restore: (username: string, available: Set<string>, defaults: SearchDraft) => void;
  reset: () => void;
}

const initial = {
  cameraIds: [],
  draft: null,
  applied: null,
  window: null,
  seekToEarliest: false,
  activeId: null,
  enlargedId: null,
  sidebarOpen: true,
  timelineOpen: true,
  notice: null,
  restoredFor: null,
};

export const usePlaybackStore = create<PlaybackState>((set, get) => ({
  ...initial,
  toggle: id => {
    const { cameraIds } = get();
    if (cameraIds.includes(id)) {
      get().remove(id);
      return;
    }
    if (cameraIds.length >= MAX_DISPLAYED_CAMERAS) {
      set({ notice: `Playback shows up to ${MAX_DISPLAYED_CAMERAS} cameras. Remove a camera to add another.` });
      return;
    }
    set({ cameraIds: [...cameraIds, id], activeId: id, notice: null });
  },
  remove: id =>
    set(state => {
      const cameraIds = state.cameraIds.filter(existing => existing !== id);
      return {
        cameraIds, notice: null,
        activeId: state.activeId === id ? (cameraIds[0] ?? null) : state.activeId,
        enlargedId: state.enlargedId === id ? null : state.enlargedId,
      };
    }),
  setDraft: draft => set(state => ({ draft: { ...(state.draft ?? { start: "", end: "", startFold: "", endFold: "" }), ...draft } })),
  apply: search => {
    playbackClock.pause();
    playbackClock.setRange(Date.parse(search.start), Date.parse(search.end));
    playbackClock.seek(Date.parse(search.start));
    set({ applied: search, window: null, seekToEarliest: true });
  },
  searchLoaded: () => set({ seekToEarliest: false }),
  setWindow: window => set({ window }),
  setActive: id => set({ activeId: id }),
  enlarge: id => set({ enlargedId: id, activeId: id }),
  unenlarge: () => set({ enlargedId: null }),
  setSidebarOpen: open => set({ sidebarOpen: open }),
  setTimelineOpen: open => set({ timelineOpen: open }),
  clearNotice: () => set({ notice: null }),
  restore: (username, available, defaults) => {
    const saved = readSaved(username);
    const cameraIds = (saved?.cameraIds ?? []).filter(id => available.has(id)).slice(0, MAX_DISPLAYED_CAMERAS);
    set({
      cameraIds,
      draft: saved?.draft ?? defaults,
      applied: saved?.applied ?? null,
      window: saved?.window ?? null,
      seekToEarliest: false,
      activeId: cameraIds[0] ?? null,
      sidebarOpen: saved?.sidebarOpen ?? true,
      timelineOpen: saved?.timelineOpen ?? true,
      restoredFor: username,
    });
    if (saved?.applied) {
      const start = Date.parse(saved.applied.start), end = Date.parse(saved.applied.end);
      playbackClock.setRange(start, end);
      playbackClock.seek(saved.time ?? start);
    }
    if (saved?.rate && (SPEEDS as readonly number[]).includes(saved.rate)) playbackClock.setRate(saved.rate);
  },
  reset: () => {
    playbackClock.reset();
    set(initial);
  },
}));

// --- remembered per user in this browser, separately from Live -----------------

interface Saved {
  cameraIds: string[];
  draft: SearchDraft;
  applied: AppliedSearch | null;
  window: TimeWindow | null;
  time: number | null;
  rate: Speed;
  sidebarOpen: boolean;
  timelineOpen: boolean;
}

const storageKey = (username: string) => `nvr-next-playback:${username}`;

function readSaved(username: string): Partial<Saved> | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(storageKey(username)) ?? "null");
    return value && typeof value === "object" ? (value as Partial<Saved>) : null;
  } catch {
    return null;
  }
}

export function savePlaybackState(): void {
  const state = usePlaybackStore.getState();
  if (!state.restoredFor || !state.draft) return;
  const saved: Saved = {
    cameraIds: state.cameraIds, draft: state.draft, applied: state.applied, window: state.window,
    time: state.applied ? playbackClock.time() : null, rate: playbackClock.rate,
    sidebarOpen: state.sidebarOpen, timelineOpen: state.timelineOpen,
  };
  try {
    localStorage.setItem(storageKey(state.restoredFor), JSON.stringify(saved));
  } catch {
    // Remembering the workspace is a convenience; private windows may refuse storage.
  }
}

// --- clock hooks ---------------------------------------------------------------

const subscribe = (listener: () => void) => playbackClock.subscribe(listener);

export function useClockTime(): number {
  return useSyncExternalStore(subscribe, () => playbackClock.current);
}

export function useClockPlaying(): boolean {
  return useSyncExternalStore(subscribe, () => playbackClock.playing);
}

export function useClockRate(): Speed {
  return useSyncExternalStore(subscribe, () => playbackClock.rate);
}

export function sameDraft(a: SearchDraft | null | undefined, b: SearchDraft | null | undefined): boolean {
  return Boolean(a && b && a.start === b.start && a.end === b.end && a.startFold === b.startFold && a.endFold === b.endFold);
}
