/**
 * The camera arrangement of a workspace, independent of React.
 *
 * Auto: `order` is the membership in display order and the grid follows its size.
 * Manual: `slots` holds fixed positions in an N×N grid (null = empty position).
 */
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import type { DewarpState } from "@/features/dewarp/geometry";

export type ManualSize = 1 | 4 | 9 | 16;

export interface Arrangement {
  mode: "auto" | "manual";
  order: string[];
  slots: (string | null)[];
}

export interface Outcome {
  arrangement: Arrangement;
  notice?: string;
}

export const EMPTY: Arrangement = { mode: "auto", order: [], slots: [] };

/** Cameras in the order they are displayed. */
export function members(arrangement: Arrangement): string[] {
  return arrangement.mode === "auto" ? arrangement.order : arrangement.slots.filter((id): id is string => id !== null);
}

export function add(arrangement: Arrangement, id: string): Outcome {
  const current = members(arrangement);
  if (current.includes(id)) return { arrangement };
  if (current.length >= MAX_DISPLAYED_CAMERAS) {
    return { arrangement, notice: `A view shows up to ${MAX_DISPLAYED_CAMERAS} cameras. Remove a camera to add another.` };
  }
  if (arrangement.mode === "manual") {
    const free = arrangement.slots.indexOf(null);
    if (free >= 0) {
      const slots = [...arrangement.slots];
      slots[free] = id;
      return { arrangement: { ...arrangement, slots } };
    }
    return {
      arrangement: { mode: "auto", order: [...current, id], slots: [] },
      notice: "The manual layout was full, so the view returned to Auto. Every camera is still shown.",
    };
  }
  return { arrangement: { ...arrangement, order: [...arrangement.order, id] } };
}

export function remove(arrangement: Arrangement, id: string): Arrangement {
  if (arrangement.mode === "manual") return { ...arrangement, slots: arrangement.slots.map(slot => (slot === id ? null : slot)) };
  return { ...arrangement, order: arrangement.order.filter(existing => existing !== id) };
}

/** Put `incoming` where `target` is. A camera already in view swaps places instead of duplicating. */
export function replace(arrangement: Arrangement, target: string, incoming: string): Arrangement {
  if (target === incoming) return arrangement;
  const current = members(arrangement);
  if (current.includes(incoming)) return swap(arrangement, target, incoming);
  const substitute = (list: (string | null)[]) => list.map(id => (id === target ? incoming : id));
  return arrangement.mode === "manual"
    ? { ...arrangement, slots: substitute(arrangement.slots) }
    : { ...arrangement, order: substitute(arrangement.order) as string[] };
}

export function swap(arrangement: Arrangement, a: string, b: string): Arrangement {
  const exchange = (list: (string | null)[]) => list.map(id => (id === a ? b : id === b ? a : id));
  return arrangement.mode === "manual"
    ? { ...arrangement, slots: exchange(arrangement.slots) }
    : { ...arrangement, order: exchange(arrangement.order) as string[] };
}

/** Place a camera in an empty manual position (drag onto an empty tile). */
export function place(arrangement: Arrangement, slot: number, id: string): Arrangement {
  if (arrangement.mode !== "manual" || arrangement.slots[slot] !== null) return arrangement;
  const slots = arrangement.slots.map(existing => (existing === id ? null : existing));
  slots[slot] = id;
  return { ...arrangement, slots };
}

/** Move a camera one position earlier or later in display order (keyboard alternative to dragging). */
export function move(arrangement: Arrangement, id: string, direction: -1 | 1): Arrangement {
  if (arrangement.mode === "manual") {
    const index = arrangement.slots.indexOf(id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= arrangement.slots.length) return arrangement;
    const slots = [...arrangement.slots];
    [slots[index], slots[target]] = [slots[target]!, slots[index]!];
    return { ...arrangement, slots };
  }
  const index = arrangement.order.indexOf(id);
  const neighbour = arrangement.order[index + direction];
  return neighbour ? swap(arrangement, id, neighbour) : arrangement;
}

export function toManual(arrangement: Arrangement, size: ManualSize): Outcome {
  const current = members(arrangement);
  if (current.length > size) {
    return { arrangement, notice: `This view has ${current.length} cameras; a ${Math.sqrt(size)}×${Math.sqrt(size)} layout holds ${size}. Remove cameras or choose a larger layout.` };
  }
  return { arrangement: { mode: "manual", order: [], slots: [...current, ...Array<null>(size - current.length).fill(null)] } };
}

export function toAuto(arrangement: Arrangement): Arrangement {
  return { mode: "auto", order: members(arrangement), slots: [] };
}

export function sameArrangement(a: Arrangement, b: Arrangement): boolean {
  if (a.mode !== b.mode) return false;
  return a.mode === "auto" ? a.order.join() === b.order.join() : a.slots.join() === b.slots.join();
}

// --- conversion to and from the saved-view API ---------------------------------

export interface SavedLayout {
  camera_ids: string[];
  grid_size: number;
  tile_camera_ids: (string | null)[];
  layout_mode: "auto" | "manual";
  dewarp: Record<string, DewarpState>;
}

export function toSaved(arrangement: Arrangement, dewarp: Record<string, DewarpState> = {}): SavedLayout {
  const ids = members(arrangement);
  const kept = Object.fromEntries(Object.entries(dewarp).filter(([id, state]) => ids.includes(id) && state.mode !== "raw"));
  if (arrangement.mode === "manual") {
    return { camera_ids: ids, grid_size: arrangement.slots.length, tile_camera_ids: arrangement.slots, layout_mode: "manual", dewarp: kept };
  }
  const size = ids.length <= 1 ? 1 : ids.length <= 4 ? 4 : ids.length <= 9 ? 9 : 16;
  return { camera_ids: ids, grid_size: size, tile_camera_ids: [...ids, ...Array<null>(size - ids.length).fill(null)], layout_mode: "auto", dewarp: kept };
}

/** Rebuild an arrangement, dropping cameras that no longer exist or are archived. */
export function fromSaved(saved: Omit<SavedLayout, "dewarp">, available: Set<string>): Arrangement {
  const keep = (id: string | null) => (id !== null && available.has(id) ? id : null);
  if (saved.layout_mode === "manual" && [1, 4, 9, 16].includes(saved.grid_size)) {
    const slots = Array.from({ length: saved.grid_size }, (_, index) => keep(saved.tile_camera_ids[index] ?? null));
    return { mode: "manual", order: [], slots };
  }
  const ordered = saved.tile_camera_ids.length ? saved.tile_camera_ids : saved.camera_ids;
  const order = ordered.map(keep).filter((id): id is string => id !== null);
  return { mode: "auto", order: [...new Set(order)].slice(0, MAX_DISPLAYED_CAMERAS), slots: [] };
}
