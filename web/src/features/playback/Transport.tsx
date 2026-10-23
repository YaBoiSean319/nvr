import type { ReactNode } from "react";
import type { DisplaySettings } from "@/api/types";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { playbackClock, SPEEDS, type Speed } from "./clock";
import { useClockPlaying, useClockRate, useClockTime, usePlaybackStore } from "./store";

/** One transport for every camera: play/pause, stop, speed and the shared time. */
export function Transport({ settings, ready, children }: { settings: DisplaySettings; ready: boolean; children?: ReactNode }) {
  const time = useClockTime();
  const playing = useClockPlaying();
  const rate = useClockRate();
  const timelineOpen = usePlaybackStore(store => store.timelineOpen);
  const { setTimelineOpen } = usePlaybackStore.getState();
  return (
    <div className="transport" role="toolbar" aria-label="Playback controls">
      <button type="button" className="button transport-play" data-variant="primary" disabled={!ready}
              onClick={() => (playing ? playbackClock.pause() : playbackClock.play())}>{playing ? "Pause" : "Play"}</button>
      <button type="button" className="button" disabled={!ready} onClick={() => playbackClock.stop()}>Stop</button>
      {children}
      <label className="transport-speed">
        <span>Speed</span>
        <select value={String(rate)} onChange={event => playbackClock.setRate(Number(event.target.value) as Speed)}>
          {SPEEDS.map(speed => <option key={speed} value={String(speed)}>{speed}×</option>)}
        </select>
      </label>
      <output className="transport-time" aria-label="Playback time" data-time={ready ? Math.round(time) : undefined}>
        {ready ? formatSiteDateTime(time, settings) : "Search a time range to play footage"}
      </output>
      <button type="button" className="button transport-timeline" data-variant="ghost" aria-expanded={timelineOpen}
              aria-controls="playback-timeline" onClick={() => setTimelineOpen(!timelineOpen)}>
        {timelineOpen ? "Hide timeline" : "Show timeline"}
      </button>
    </div>
  );
}
