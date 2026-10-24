/** Pure time-axis arithmetic for the playback timeline. */
import type { TimeWindow } from "./store";

export const MIN_WINDOW_MS = 10_000;

const STEPS = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600].map(seconds => seconds * 1000);

/** A tick spacing that leaves roughly `minPx` between labels. */
export function tickStep(window: TimeWindow, widthPx: number, minPx = 90): number {
  const span = window.end - window.start;
  const wanted = (span * minPx) / Math.max(1, widthPx);
  return STEPS.find(step => step >= wanted) ?? STEPS[STEPS.length - 1]!;
}

/** Tick instants inside the window, aligned to the step in the site's offset from UTC. */
export function ticks(window: TimeWindow, step: number, offsetMs: number): number[] {
  const first = Math.ceil((window.start + offsetMs) / step) * step - offsetMs;
  const result: number[] = [];
  for (let time = first; time <= window.end; time += step) result.push(time);
  return result;
}

function clampWindow(start: number, span: number, range: TimeWindow): TimeWindow {
  const width = Math.min(span, range.end - range.start);
  const from = Math.max(range.start, Math.min(range.end - width, start));
  return { start: from, end: from + width };
}

/** Zoom by `factor` (<1 in, >1 out) keeping `focus` at the same place on screen. */
export function zoom(window: TimeWindow, range: TimeWindow, factor: number, focus: number): TimeWindow {
  const span = Math.max(MIN_WINDOW_MS, (window.end - window.start) * factor);
  const ratio = (focus - window.start) / (window.end - window.start || 1);
  return clampWindow(focus - span * ratio, span, range);
}

export function pan(window: TimeWindow, range: TimeWindow, delta: number): TimeWindow {
  return clampWindow(window.start + delta, window.end - window.start, range);
}

export function isWholeRange(window: TimeWindow, range: TimeWindow): boolean {
  return window.start <= range.start && window.end >= range.end;
}

/** Percent position of an instant, and the visible part of an interval, within the window. */
export function percent(time: number, window: TimeWindow): number {
  return ((time - window.start) / (window.end - window.start)) * 100;
}

export function visibleSpan(start: number, end: number, window: TimeWindow): { left: number; width: number } | null {
  const from = Math.max(start, window.start), to = Math.min(end, window.end);
  if (to <= from) return null;
  return { left: percent(from, window), width: percent(to, window) - percent(from, window) };
}
