/**
 * Bandwidth-aware viewing (#134): Auto quality that steps a tile between the main stream and the substream from how
 * its video is actually arriving, and a low-data mode that holds everything to substreams and stops what nobody sees.
 */
import { create } from "zustand";

export type LowDataSetting = "auto" | "on" | "off";

export interface ViewingPrefs {
  lowData: LowDataSetting;
  /** Auto quality changes a tile's stream at most this often. */
  intervalSeconds: number;
}

export const QUALITY_INTERVALS = [15, 30, 60, 120] as const;
/** In low-data mode this many tiles play (in view order); the rest wait for a tap. */
export const LOW_DATA_MAX_TILES = 4;

const STORAGE_KEY = "nvr.viewing";
const DEFAULTS: ViewingPrefs = { lowData: "auto", intervalSeconds: 30 };

function load(): ViewingPrefs {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null") as Partial<ViewingPrefs> | null;
    return {
      lowData: saved?.lowData === "on" || saved?.lowData === "off" ? saved.lowData : "auto",
      intervalSeconds: typeof saved?.intervalSeconds === "number" && saved.intervalSeconds >= 1 ? saved.intervalSeconds : DEFAULTS.intervalSeconds,
    };
  } catch {
    return DEFAULTS;
  }
}

function save(prefs: ViewingPrefs): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Private windows and blocked storage just forget the choice.
  }
}

interface PrefsState extends ViewingPrefs {
  set: (patch: Partial<ViewingPrefs>) => void;
}

export const useViewingPrefs = create<PrefsState>(set => ({
  ...load(),
  set: patch => set(state => {
    const next = { lowData: state.lowData, intervalSeconds: state.intervalSeconds, ...patch };
    save(next);
    return next;
  }),
}));

interface NetworkInformation {
  type?: string;
  saveData?: boolean;
}

/** Low data on its own: the installed app on a cellular connection, where the browser says so (or asks for less data). */
export function automaticLowData(): boolean {
  const connection = (navigator as Navigator & { connection?: NetworkInformation }).connection;
  if (!connection) return false;
  const installed = window.matchMedia?.("(display-mode: standalone)").matches || window.matchMedia?.("(display-mode: window-controls-overlay)").matches;
  return Boolean(installed && (connection.type === "cellular" || connection.saveData));
}

export function lowDataActive(setting: LowDataSetting): boolean {
  return setting === "on" || (setting === "auto" && automaticLowData());
}

export function useLowData(): boolean {
  return lowDataActive(useViewingPrefs(state => state.lowData));
}

export interface QualityChange {
  quality: "low" | "high";
  reason: string;
}

export interface GovernorOptions {
  intervalMs: number;
  /** Video fragments more than this far apart, or a stall, count as arriving late. */
  lateGapMs?: number;
  /** This many late arrivals within the window step down. */
  lateCount?: number;
  windowMs?: number;
  /** How long without any late arrival before trying the main stream again (default twice the interval, never less than it). */
  calmMs?: number;
}

/**
 * Decides when a tile in Auto moves between the main stream and the substream. Lateness steps down; a calm spell steps back
 * up, and a step up that goes badly makes the next wait longer. No change comes sooner than the interval after the last
 * one, so a connection hovering at the edge cannot make the tile flap.
 */
export class QualityGovernor {
  private current: "low" | "high";
  private changedAt: number;
  private late: number[] = [];
  private lastFragment: number | null = null;
  private lowered = false;
  private calmFactor = 1;
  private recentlyRaised = false;
  private readonly lateGapMs: number;
  private readonly lateCount: number;
  private readonly windowMs: number;
  private readonly baseCalmMs: number;

  constructor(start: "low" | "high", private readonly options: GovernorOptions, now: number) {
    this.current = start;
    this.changedAt = now;
    this.lateGapMs = options.lateGapMs ?? 2_000;
    this.lateCount = options.lateCount ?? 2;
    this.windowMs = options.windowMs ?? 15_000;
    this.baseCalmMs = Math.max(options.calmMs ?? options.intervalMs * 2, options.intervalMs);
  }

  get quality(): "low" | "high" {
    return this.current;
  }

  /** A video fragment arrived. */
  fragment(at: number): void {
    if (this.lastFragment !== null && at - this.lastFragment > this.lateGapMs) this.late.push(at);
    this.lastFragment = at;
  }

  /** Playback stopped to wait for data. */
  stall(at: number): void {
    this.late.push(at);
  }

  /** Called regularly: the change to make now, if any. */
  check(at: number): QualityChange | null {
    this.late = this.late.filter(time => at - time <= this.windowMs);
    if (at - this.changedAt < this.options.intervalMs) return null;
    if (this.current === "high" && this.late.length >= this.lateCount) {
      if (this.recentlyRaised) this.calmFactor = Math.min(this.calmFactor * 2, 8);
      return this.change("low", at, "Connection is slow: video was arriving late");
    }
    if (this.current === "low" && this.lowered) {
      const lastLate = this.late.length ? this.late[this.late.length - 1]! : 0;
      if (at - Math.max(this.changedAt, lastLate) >= this.baseCalmMs * this.calmFactor) {
        return this.change("high", at, "Connection recovered");
      }
    }
    if (this.current === "high" && this.recentlyRaised && at - this.changedAt >= this.windowMs + this.baseCalmMs) this.calmFactor = 1; // the step up held
    return null;
  }

  private change(quality: "low" | "high", at: number, reason: string): QualityChange {
    this.current = quality;
    this.changedAt = at;
    this.late = [];
    this.lastFragment = null; // the new stream's first fragment is not late
    this.lowered = quality === "low";
    this.recentlyRaised = quality === "high";
    return { quality, reason };
  }
}
