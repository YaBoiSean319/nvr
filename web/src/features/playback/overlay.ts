/**
 * Boxes and trails over playback, from the detections the AI worker stored, never from live inference.
 * Everything here is pure: the playback clock's time goes in, what to draw comes out.
 */
export interface OverlayBox { track_id: string | null; label: string; confidence: number; box: { x: number; y: number; width: number; height: number } }
export interface OverlayFrame { at: number; boxes: OverlayBox[] }
export interface TrackPoint { at: number; run?: number; box: { x: number; y: number; width: number; height: number } }

const MIN_HOLD_MS = 1000;
const MAX_HOLD_MS = 6000;
const DEFAULT_HOLD_MS = 3000;

/**
 * How long one analysed frame stays on screen: a little over two analysis intervals, worked out from the frames themselves (the typical
 * gap between neighbours), so a camera analysed twice a second and one analysed every two seconds both draw boxes that follow the video
 * and neither keeps a box on screen long after the object has gone.
 */
export function holdFor(times: number[]): number {
  const gaps: number[] = [];
  for (let index = 1; index < times.length; index += 1) {
    const gap = times[index]! - times[index - 1]!;
    if (gap > 0 && gap < MAX_HOLD_MS * 2) gaps.push(gap);
  }
  if (!gaps.length) return DEFAULT_HOLD_MS;
  gaps.sort((a, b) => a - b);
  const median = gaps[Math.floor(gaps.length / 2)]!;
  return Math.min(MAX_HOLD_MS, Math.max(MIN_HOLD_MS, 2.5 * median));
}

/** The last index whose time is at or before ``time``, or -1. */
export function lastAtOrBefore(times: number[], time: number): number {
  let low = 0;
  let high = times.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (times[middle]! <= time) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

/** The analysed frame that is on screen at ``time``: the latest one at or before it, if it is recent enough (``hold``) and not an earlier gap. */
export function frameAt(frames: OverlayFrame[], time: number, hold?: number, times = frames.map(frame => frame.at)): OverlayFrame | null {
  const index = lastAtOrBefore(times, time);
  if (index < 0) return null;
  const frame = frames[index]!;
  return time - frame.at <= (hold ?? holdFor(times)) ? frame : null;
}

export interface TrailSegment { from: { x: number; y: number }; to: { x: number; y: number }; opacity: number; at: number }

export const TRAIL_MS = 12_000;

/** The centre of a box as fractions of the picture. */
export function centre(box: TrackPoint["box"]): { x: number; y: number } {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

/**
 * A fading trail of where an object has been: segments between its stored centres up to ``time`` over the last ``span``, each as faint
 * as it is old. A pause in the points (an occlusion) is bridged, since the path is the same object's, but never across a privacy-masked area.
 */
export function trail(points: TrackPoint[], time: number, span = TRAIL_MS): TrailSegment[] {
  const last = lastAtOrBefore(points.map(point => point.at), time);
  const segments: TrailSegment[] = [];
  for (let index = last; index > 0; index -= 1) {
    const to = points[index]!;
    if (time - to.at > span) break;
    const from = points[index - 1]!;
    if ((from.run ?? 0) !== (to.run ?? 0)) break; // The path went through a privacy-masked area here: nothing is drawn across it
    segments.push({ from: centre(from.box), to: centre(to.box), at: to.at, opacity: Math.max(0.1, 1 - (time - to.at) / span) });
  }
  return segments.reverse();
}

export type Direction = "previous" | "next";
const SAME_INSTANT_MS = 1;

/** The time of the object's previous or next stored appearance, stepping across any pause in between; null when there is none. */
export function neighbour(points: TrackPoint[], time: number, direction: Direction): number | null {
  const times = points.map(point => point.at);
  if (direction === "next") {
    const index = lastAtOrBefore(times, time + SAME_INSTANT_MS);
    return index + 1 < times.length ? times[index + 1]! : null;
  }
  const index = lastAtOrBefore(times, time - SAME_INSTANT_MS);
  return index >= 0 ? times[index]! : null;
}

/** Where the picture sits inside its tile when the video is fitted (letterboxed), as pixels from the tile's top left. */
export function pictureRect(tile: { width: number; height: number }, video: { width: number; height: number }): { left: number; top: number; width: number; height: number } {
  if (!tile.width || !tile.height || !video.width || !video.height) return { left: 0, top: 0, width: tile.width, height: tile.height };
  const scale = Math.min(tile.width / video.width, tile.height / video.height);
  const width = video.width * scale;
  const height = video.height * scale;
  return { left: (tile.width - width) / 2, top: (tile.height - height) / 2, width, height };
}
