import { create } from "zustand";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";

export type Quality = "auto" | "main" | "sub";

interface LiveState {
  cameraIds: string[];
  activeId: string | null;
  enlargedId: string | null;
  sidebarOpen: boolean;
  quality: Record<string, Quality>;
  notice: string | null;
  /** The one camera that may be audible; audio always starts muted. */
  listeningId: string | null;
  setListening: (id: string | null) => void;
  /** Add or remove one camera; new cameras append and keep everyone else's position. */
  toggle: (id: string) => void;
  remove: (id: string) => void;
  setActive: (id: string) => void;
  enlarge: (id: string) => void;
  restore: () => void;
  setQuality: (id: string, quality: Quality) => void;
  setSidebarOpen: (open: boolean) => void;
  clearNotice: () => void;
}

export const useLiveStore = create<LiveState>((set, get) => ({
  cameraIds: [],
  activeId: null,
  enlargedId: null,
  sidebarOpen: true,
  quality: {},
  notice: null,
  listeningId: null,
  setListening: id => set({ listeningId: id }),
  toggle: id => {
    const { cameraIds } = get();
    if (cameraIds.includes(id)) {
      get().remove(id);
      return;
    }
    if (cameraIds.length >= MAX_DISPLAYED_CAMERAS) {
      set({ notice: `Live shows up to ${MAX_DISPLAYED_CAMERAS} cameras at once. Remove a camera to add another.` });
      return;
    }
    set({ cameraIds: [...cameraIds, id], activeId: id, notice: null });
  },
  remove: id =>
    set(state => {
      const cameraIds = state.cameraIds.filter(existing => existing !== id);
      const index = state.cameraIds.indexOf(id);
      // Removing the active tile hands control to its neighbour.
      const activeId = state.activeId === id ? (cameraIds[Math.min(index, cameraIds.length - 1)] ?? null) : state.activeId;
      return {
        cameraIds, activeId, notice: null,
        enlargedId: state.enlargedId === id ? null : state.enlargedId,
        listeningId: state.listeningId === id ? null : state.listeningId,
      };
    }),
  setActive: id => set({ activeId: id }),
  enlarge: id => set({ enlargedId: id, activeId: id }),
  restore: () => set({ enlargedId: null }),
  setQuality: (id, quality) => set(state => ({ quality: { ...state.quality, [id]: quality } })),
  setSidebarOpen: open => set({ sidebarOpen: open }),
  clearNotice: () => set({ notice: null }),
}));
