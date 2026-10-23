import { create } from "zustand";
import { DEFAULT_FILTERS, DEFAULT_SORT, type Filters, type Sort } from "./inventory";

export type EditorTab = "general" | "recording" | "motion" | "ai" | "schedule" | "health";

interface Preferences {
  sort: Sort;
  showBrand: boolean;
  /** Inventory pane share of the width, percent. */
  paneWidth: number;
}

interface CamerasState extends Preferences {
  /** The one camera whose editor is open; never chosen automatically. */
  selectedId: string | null;
  tab: EditorTab;
  /** Bulk selection, independent of the editor. */
  checked: string[];
  filters: Filters;
  previewing: boolean;
  /** Phones show the editor over the inventory until Back; the selection stays. */
  detailOpen: boolean;
  preferencesFor: string | null;

  select: (id: string | null) => void;
  setTab: (tab: EditorTab) => void;
  toggleChecked: (id: string) => void;
  setChecked: (ids: string[]) => void;
  setFilters: (filters: Partial<Filters>) => void;
  setSort: (sort: Sort) => void;
  setShowBrand: (show: boolean) => void;
  setPaneWidth: (width: number) => void;
  setPreviewing: (on: boolean) => void;
  closeDetail: () => void;
  loadPreferences: (username: string) => void;
  reset: () => void;
}

const DEFAULT_PREFERENCES: Preferences = { sort: DEFAULT_SORT, showBrand: false, paneWidth: 55 };

const initial = {
  ...DEFAULT_PREFERENCES,
  selectedId: null,
  tab: "general" as EditorTab,
  checked: [],
  filters: DEFAULT_FILTERS,
  previewing: false,
  detailOpen: false,
  preferencesFor: null,
};

const key = (username: string) => `nvr-next-camera-table:${username}`;

function persist(state: CamerasState) {
  if (!state.preferencesFor) return;
  try {
    // Only harmless table preferences; never selection, drafts or anything secret.
    localStorage.setItem(key(state.preferencesFor), JSON.stringify({ sort: state.sort, showBrand: state.showBrand, paneWidth: state.paneWidth }));
  } catch {
    // Preferences are a convenience.
  }
}

export const useCamerasStore = create<CamerasState>((set, get) => ({
  ...initial,
  select: id => set(state => ({
    selectedId: id, detailOpen: id !== null,
    previewing: id === state.selectedId ? state.previewing : false,
    tab: id === state.selectedId ? state.tab : "general",
  })),
  closeDetail: () => set({ detailOpen: false }),
  setTab: tab => set({ tab }),
  toggleChecked: id => set(state => ({ checked: state.checked.includes(id) ? state.checked.filter(existing => existing !== id) : [...state.checked, id] })),
  setChecked: checked => set({ checked }),
  setFilters: filters => set(state => ({ filters: { ...state.filters, ...filters } })),
  setSort: sort => { set({ sort }); persist(get()); },
  setShowBrand: showBrand => { set({ showBrand }); persist(get()); },
  setPaneWidth: paneWidth => { set({ paneWidth: Math.max(30, Math.min(75, paneWidth)) }); persist(get()); },
  setPreviewing: previewing => set({ previewing }),
  loadPreferences: username => {
    let saved: Partial<Preferences> = {};
    try {
      saved = JSON.parse(localStorage.getItem(key(username)) ?? "{}") as Partial<Preferences>;
    } catch {
      saved = {};
    }
    const sort = saved.sort && ["name", "ip", "group", "connection", "health", "mode", "brand"].includes(saved.sort.key) && [1, -1].includes(saved.sort.direction)
      ? saved.sort : DEFAULT_SORT;
    set({
      sort,
      showBrand: saved.showBrand === true,
      paneWidth: typeof saved.paneWidth === "number" ? Math.max(30, Math.min(75, saved.paneWidth)) : DEFAULT_PREFERENCES.paneWidth,
      preferencesFor: username,
    });
  },
  reset: () => set(initial),
}));
