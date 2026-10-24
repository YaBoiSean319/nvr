/**
 * What a video wall shows, independent of React: a rotation of shared views, which
 * an alarm interrupts. Alarms that arrive while one is shown wait their turn.
 */
import type { PopIn } from "@/features/live/store";

export const DEFAULT_DWELL_SECONDS = 30;
export const MIN_DWELL_SECONDS = 5;
export const MAX_DWELL_SECONDS = 3600;
/** An alarm stays on the wall at least this long, whatever the rotation's dwell. */
export const MIN_ALARM_SECONDS = 20;

export interface WallState {
  viewIndex: number;
  alarm: PopIn | null;
  queue: PopIn[];
}

export type WallAction =
  | { type: "rotate"; viewCount: number }
  | { type: "alarm"; alarm: PopIn }
  | { type: "alarmDone" };

export const INITIAL_WALL: WallState = { viewIndex: 0, alarm: null, queue: [] };

export function wallReducer(state: WallState, action: WallAction): WallState {
  switch (action.type) {
    case "rotate":
      // The rotation pauses while an alarm is shown.
      if (state.alarm || action.viewCount < 2) return state;
      return { ...state, viewIndex: (state.viewIndex + 1) % action.viewCount };
    case "alarm":
      if (state.alarm?.id === action.alarm.id || state.queue.some(waiting => waiting.id === action.alarm.id)) return state;
      return state.alarm ? { ...state, queue: [...state.queue, action.alarm] } : { ...state, alarm: action.alarm };
    case "alarmDone": {
      const [next = null, ...rest] = state.queue;
      return { ...state, alarm: next, queue: rest };
    }
  }
}

export interface WallAddress {
  views: string[];
  dwellSeconds: number;
}

/** `?view=Lobby&view=Yard&dwell=30`: the views to rotate, in order, and seconds on each. */
export function parseWallAddress(search: string): WallAddress {
  const params = new URLSearchParams(search);
  const views = [...new Set(params.getAll("view").map(name => name.trim()).filter(Boolean))];
  const dwell = Number(params.get("dwell"));
  const dwellSeconds = Number.isFinite(dwell) && dwell > 0 ? Math.min(MAX_DWELL_SECONDS, Math.max(MIN_DWELL_SECONDS, Math.round(dwell))) : DEFAULT_DWELL_SECONDS;
  return { views, dwellSeconds };
}

export function wallSearch({ views, dwellSeconds }: WallAddress): string {
  const params = new URLSearchParams();
  views.forEach(view => params.append("view", view));
  params.set("dwell", String(dwellSeconds));
  return `?${params.toString()}`;
}
