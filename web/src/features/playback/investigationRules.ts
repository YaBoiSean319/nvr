/** Investigation rules for Playback: events, empty periods and frame neighbours. */
import { api, ApiError } from "@/api/client";
import type { TimelineCamera } from "@/api/types";

/** Events this close to the playhead count as "under" it, and this close to each other as one. */
export const EVENT_TOLERANCE_MS = 250;

export type Direction = "previous" | "next";

interface Interval { id: number; start: number; end: number }

export function intervalsOf(camera: TimelineCamera): Interval[] {
  return camera.segments
    .map(segment => ({ id: segment.id, start: Date.parse(segment.starts_at), end: Date.parse(segment.ends_at) }))
    .sort((a, b) => a.start - b.start);
}

export function intervalAt(camera: TimelineCamera, time: number): Interval | null {
  return intervalsOf(camera).find(interval => interval.start <= time && time < interval.end) ?? null;
}

/**
 * Skip Empty Periods: time with footage on any searched camera plays as usual (recording
 * is what counts, motion or not); time empty on every camera jumps to the next footage.
 */
export function skipEmpty(cameras: TimelineCamera[]): (time: number) => number | null {
  const all = cameras.flatMap(intervalsOf);
  return time => {
    if (all.some(interval => interval.start <= time && time < interval.end)) return time;
    const later = all.filter(interval => interval.start > time).map(interval => interval.start);
    return later.length ? Math.min(...later) : null;
  };
}

/** Motion event start times, simultaneous starts merged into one destination. */
export function eventTimes(cameras: TimelineCamera[], range: { start: number; end: number }): number[] {
  const times = cameras
    .flatMap(camera => camera.motion_events.map(event => Date.parse(event.source_event_at)))
    .filter(time => time >= range.start && time < range.end)
    .sort((a, b) => a - b);
  return times.filter((time, index) => index === 0 || time - times[index - 1]! > EVENT_TOLERANCE_MS);
}

/** The next or previous event, skipping one under the playhead and never wrapping. */
export function eventDestination(times: number[], at: number, direction: Direction): number | null {
  if (direction === "next") return times.find(time => time > at + EVENT_TOLERANCE_MS) ?? null;
  return [...times].reverse().find(time => time < at - EVENT_TOLERANCE_MS) ?? null;
}

/** An instant with sub-millisecond precision as ISO 8601 with microseconds (Date drops them). */
export function toMicroIso(ms: number): string {
  const whole = Math.floor(ms);
  const micros = Math.round((ms - whole) * 1000);
  const base = new Date(whole).toISOString(); // …SS.mmmZ
  return `${base.slice(0, -1)}${String(Math.min(999, micros)).padStart(3, "0")}Z`;
}

/** Parse ISO 8601 keeping microseconds as a fractional millisecond. */
export function parseMicroIso(iso: string): number {
  const match = /\.(\d+)/.exec(iso);
  if (!match) return Date.parse(iso);
  const fraction = Number(`0.${match[1]}`) * 1000;
  return Date.parse(iso.replace(/\.\d+/, "")) + fraction;
}

const NO_NEIGHBOUR = new Set(["No adjacent frame exists in this interval", "Frame time is outside this recorded interval"]);

/**
 * The active camera's actual neighbouring source frame, searching the interval under the
 * playhead first and then earlier or later intervals, within the searched range.
 */
export async function frameNeighbour(camera: TimelineCamera, at: number, direction: Direction, range: { start: number; end: number },
                                     signal: AbortSignal): Promise<number | null> {
  const intervals = intervalsOf(camera);
  const ordered = direction === "next"
    ? intervals.filter(interval => interval.end > at)
    : intervals.filter(interval => interval.start < at).reverse();
  const neighbour = async (interval: Interval, from: number, way: Direction): Promise<number | null> => {
    const query = new URLSearchParams({ at: toMicroIso(from), direction: way });
    try {
      const result = await api<{ at: string }>(`/api/cameras/${encodeURIComponent(camera.camera_id)}/segments/${interval.id}/frame-neighbor?${query}`, { signal });
      return parseMicroIso(result.at);
    } catch (error) {
      if (error instanceof ApiError && NO_NEIGHBOUR.has(error.detail)) return null;
      throw error;
    }
  };
  for (const interval of ordered) {
    const requested = Math.max(interval.start, Math.min(interval.end - 1, at));
    let found = await neighbour(interval, requested, direction);
    // Entering an interval from before it: the frame after its start is the second frame,
    // so step back once to land on the interval's real first frame.
    if (found !== null && direction === "next" && at < interval.start) found = (await neighbour(interval, found, "previous")) ?? found;
    if (found === null) continue;
    const beyond = direction === "next" ? found > at : found < at;
    if (beyond) return found >= range.start && found < range.end ? found : null;
    // The interval's first or last frame sits exactly on the playhead: look further.
  }
  return null;
}
