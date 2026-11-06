/**
 * Boxes and trails over playback, from the detections the AI worker stored, never from live inference.
 * Everything here is pure: the playback clock's time goes in, what to draw comes out.
 */
/** Body points the pose model saw, by name, as [x, y] in fractions of the picture. */
export type Pose = Record<string, [number, number]>;
export interface OverlayBox { track_id: string | null; label: string; confidence: number; box: { x: number; y: number; width: number; height: number }; pose?: Pose }
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

type Box = TrackPoint["box"];

const lerp = (from: number, to: number, progress: number) => from + (to - from) * progress;

/** Each point the person has in both poses moves toward its later place; a point seen in only one stays where it was. */
function blendPose(from: Pose, to: Pose, progress: number): Pose {
  const blended: Pose = {};
  for (const [name, point] of Object.entries(from)) {
    const later = to[name];
    blended[name] = later ? [lerp(point[0], later[0], progress), lerp(point[1], later[1], progress)] : point;
  }
  return blended;
}

/** The lines of a body outline: pairs of named points, and the neck from the nose to the middle of the shoulders. */
const BONES: [string, string][] = [
  ["left_shoulder", "right_shoulder"], ["left_shoulder", "left_hip"], ["right_shoulder", "right_hip"], ["left_hip", "right_hip"],
  ["left_shoulder", "left_elbow"], ["left_elbow", "left_wrist"], ["right_shoulder", "right_elbow"], ["right_elbow", "right_wrist"],
  ["left_hip", "left_knee"], ["left_knee", "left_ankle"], ["right_hip", "right_knee"], ["right_knee", "right_ankle"],
];

export interface BoneLine { x1: number; y1: number; x2: number; y2: number; key: string }

/** The lines to draw for one person's pose: a bone only where both of its ends were seen. */
export function skeletonLines(pose: Pose): BoneLine[] {
  const lines: BoneLine[] = [];
  for (const [a, b] of BONES) {
    const from = pose[a];
    const to = pose[b];
    if (from && to) lines.push({ x1: from[0], y1: from[1], x2: to[0], y2: to[1], key: `${a}-${b}` });
  }
  const left = pose.left_shoulder;
  const right = pose.right_shoulder;
  const nose = pose.nose;
  if (nose && left && right) lines.push({ x1: nose[0], y1: nose[1], x2: (left[0] + right[0]) / 2, y2: (left[1] + right[1]) / 2, key: "neck" });
  return lines;
}

/**
 * The frame to draw at ``time``, with each followed object placed between where it was in this analysed frame and where it is in the next one,
 * as far through as the clock is. The detections are analysed a few times a second at most, so drawing each box where it was last seen leaves it
 * behind a moving person by up to a whole interval; playback has the later detection too, so the box can be where the person was at this moment.
 * An object that is not in the next frame (it left, or was hidden), a gap that is not an ordinary interval, and a box with no track stay as stored.
 */
export function interpolatedFrameAt(frames: OverlayFrame[], time: number, hold?: number, times = frames.map(frame => frame.at)): OverlayFrame | null {
  const frame = frameAt(frames, time, hold, times);
  if (!frame) return null;
  const following = frames[lastAtOrBefore(times, time) + 1];
  const limit = hold ?? holdFor(times);
  if (!following || following.at <= frame.at || following.at - frame.at > limit || time <= frame.at) return frame;
  const progress = Math.min(1, (time - frame.at) / (following.at - frame.at));
  const boxes = frame.boxes.map(item => {
    const later = item.track_id ? following.boxes.find(candidate => candidate.track_id === item.track_id && candidate.label === item.label) : undefined;
    if (!later) return item;
    const box: Box = { x: lerp(item.box.x, later.box.x, progress), y: lerp(item.box.y, later.box.y, progress), width: lerp(item.box.width, later.box.width, progress), height: lerp(item.box.height, later.box.height, progress) };
    return item.pose && later.pose ? { ...item, box, pose: blendPose(item.pose, later.pose, progress) } : { ...item, box };
  });
  return { at: frame.at, boxes };
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
