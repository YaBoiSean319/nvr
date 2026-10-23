/**
 * The one shared playback clock. Every tile, the timeline cursor and the transport
 * read time from here, so all cameras follow the same instant at every speed.
 *
 * Time advances from wall-clock elapsed time × rate, not from any video, so gaps
 * (where no video plays) keep moving and a stalled tile can never hold others back.
 */
export const SPEEDS = [0.25, 0.5, 1, 2, 4, 8, 16] as const;
export type Speed = (typeof SPEEDS)[number];

const TICK_MS = 100;

type Listener = () => void;

export class PlaybackClock {
  private anchorTime = 0;
  private anchorWall = 0;
  private ticker: ReturnType<typeof setInterval> | null = null;
  private listeners = new Set<Listener>();
  private start = 0;
  private end = 0;
  playing = false;
  rate: Speed = 1;
  /** The time at the most recent notification; stable between notifications for React snapshots. */
  current = 0;

  constructor(private readonly now: () => number = () => performance.now()) {}

  time(): number {
    if (!this.playing) return this.anchorTime;
    return Math.min(this.end, this.anchorTime + (this.now() - this.anchorWall) * this.rate);
  }

  get range(): { start: number; end: number } {
    return { start: this.start, end: this.end };
  }

  /** Bound the clock to a search range, keeping the current time when it still fits. */
  setRange(start: number, end: number): void {
    this.start = start;
    this.end = end;
    this.reanchor(Math.max(start, Math.min(end, this.time())));
    this.emit();
  }

  seek(time: number): void {
    this.reanchor(Math.max(this.start, Math.min(this.end, time)));
    this.emit();
  }

  play(): void {
    if (this.playing || this.end <= this.start) return;
    const at = this.time() >= this.end ? this.start : this.time();
    this.playing = true;
    this.reanchor(at);
    this.ticker = setInterval(() => this.tick(), TICK_MS);
    this.emit();
  }

  pause(): void {
    if (!this.playing) return;
    const at = this.time();
    this.playing = false;
    this.reanchor(at);
    if (this.ticker) clearInterval(this.ticker);
    this.ticker = null;
    this.emit();
  }

  stop(): void {
    this.pause();
    this.seek(this.start);
  }

  setRate(rate: Speed): void {
    this.reanchor(this.time());
    this.rate = rate;
    this.emit();
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Stop and forget everything (sign-out). */
  reset(): void {
    this.pause();
    this.start = this.end = this.anchorTime = this.current = 0;
    this.rate = 1;
    this.emit();
  }

  private tick(): void {
    if (this.time() >= this.end) {
      this.pause();
      this.seek(this.end);
      return;
    }
    this.emit();
  }

  private reanchor(time: number): void {
    this.anchorTime = time;
    this.anchorWall = this.now();
  }

  private emit(): void {
    this.current = this.time();
    this.listeners.forEach(listener => listener());
  }
}

export const playbackClock = new PlaybackClock();
