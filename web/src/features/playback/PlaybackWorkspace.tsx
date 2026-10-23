import { useEffect, useMemo } from "react";
import { ApiError } from "@/api/client";
import { useCameras, useDisplaySettings, useMe, usePlaybackTimeline } from "@/api/queries";
import type { TimelineCamera } from "@/api/types";
import { BrowserFrame, CameraList } from "@/features/cameras/CameraBrowser";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { toLocalInput } from "@/lib/zonedTime";
import { playbackClock } from "./clock";
import { PlaybackGrid } from "./PlaybackGrid";
import { SearchBar } from "./SearchBar";
import { savePlaybackState, usePlaybackStore } from "./store";
import { Timeline } from "./Timeline";
import { Transport } from "./Transport";

const SAVE_DELAY_MS = 750;

/** Remember the investigation in this browser at most once per short interval, and when leaving. */
function usePlaybackPersistence(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const schedule = () => {
      timer ??= setTimeout(() => {
        timer = null;
        savePlaybackState();
      }, SAVE_DELAY_MS);
    };
    const unsubscribeStore = usePlaybackStore.subscribe(schedule);
    const unsubscribeClock = playbackClock.subscribe(schedule);
    return () => {
      unsubscribeStore();
      unsubscribeClock();
      if (timer) clearTimeout(timer);
      savePlaybackState();
    };
  }, [enabled]);
}

export function PlaybackWorkspace() {
  const cameras = useCameras();
  const me = useMe();
  const display = useDisplaySettings();
  const restoredFor = usePlaybackStore(store => store.restoredFor);
  const cameraIds = usePlaybackStore(store => store.cameraIds);
  const applied = usePlaybackStore(store => store.applied);
  const seekToEarliest = usePlaybackStore(store => store.seekToEarliest);
  const sidebarOpen = usePlaybackStore(store => store.sidebarOpen);
  const timelineOpen = usePlaybackStore(store => store.timelineOpen);
  const notice = usePlaybackStore(store => store.notice);
  const { toggle, setSidebarOpen, clearNotice, searchLoaded } = usePlaybackStore.getState();
  const byId = useMemo(() => new Map((cameras.data ?? []).filter(camera => !camera.archived_at).map(camera => [camera.id, camera])), [cameras.data]);
  const shownIds = useMemo(() => cameraIds.filter(id => byId.has(id)), [cameraIds, byId]);
  const query = usePlaybackTimeline(shownIds, applied);
  const timeline = useMemo(() => new Map<string, TimelineCamera>((query.data?.cameras ?? []).map(row => [row.camera_id, row])), [query.data]);
  const username = me.data?.username;
  const ready = restoredFor !== null && restoredFor === username;

  // Restore this person's investigation once cameras and the site time zone are known.
  useEffect(() => {
    if (!username || !cameras.data || !display.data || restoredFor === username) return;
    const now = Date.now();
    const zone = display.data.time_zone;
    usePlaybackStore.getState().restore(username, new Set(byId.keys()), {
      start: toLocalInput(now - 3_600_000, zone), end: toLocalInput(now, zone), startFold: "", endFold: "",
    });
  }, [username, cameras.data, display.data, restoredFor, byId]);

  usePlaybackPersistence(ready);

  // Leaving Playback stops the clock; the position is remembered.
  useEffect(() => () => playbackClock.pause(), []);

  // A new search starts paused at the earliest recording among the searched cameras.
  useEffect(() => {
    const data = query.data;
    if (!seekToEarliest || !applied || !data || query.isPlaceholderData) return;
    if (Date.parse(data.start) !== Date.parse(applied.start) || Date.parse(data.end) !== Date.parse(applied.end)) return;
    const starts = data.cameras.flatMap(row => row.segments.map(segment => Math.max(Date.parse(segment.starts_at), Date.parse(applied.start))));
    playbackClock.pause();
    playbackClock.seek(starts.length ? Math.min(...starts) : Date.parse(applied.start));
    searchLoaded();
  }, [query.data, query.isPlaceholderData, applied, seekToEarliest, searchLoaded]);

  const camerasError = cameras.error instanceof ApiError ? cameras.error.detail : cameras.error ? "Cameras could not be loaded." : null;
  const timelineError = query.error instanceof ApiError ? query.error.detail : query.error ? "The timeline could not be loaded." : null;
  const range = applied ? { start: Date.parse(applied.start), end: Date.parse(applied.end) } : null;

  return (
    <div className="workspace playback-workspace" data-sidebar={sidebarOpen ? "open" : "closed"}>
      {sidebarOpen ? (
        <BrowserFrame onCollapse={() => setSidebarOpen(false)}>
          <div className="browser-heading">Cameras</div>
          <CameraList cameras={cameras.data} loading={cameras.isPending} error={camerasError} selected={shownIds}
                      onToggle={toggle} capacity={MAX_DISPLAYED_CAMERAS} />
        </BrowserFrame>
      ) : null}
      <section className="canvas playback-canvas" aria-label="Playback" data-timeline={timelineOpen ? "open" : "closed"}>
        <div className="canvas-bar">
          {!sidebarOpen ? <button type="button" className="button" onClick={() => setSidebarOpen(true)} aria-label="Show camera browser">Cameras</button> : null}
          <span className="canvas-title">Playback</span>
          {ready && display.data ? <SearchBar settings={display.data} /> : null}
          {notice ? (
            <p className="canvas-notice" role="status">
              {notice}
              <button type="button" className="button" data-variant="ghost" onClick={clearNotice} aria-label="Dismiss">✕</button>
            </p>
          ) : null}
        </div>
        <div className="playback-video">
          {timelineError ? <p className="notice playback-error" data-tone="bad" role="alert">{timelineError}</p> : null}
          {ready ? <PlaybackGrid cameras={byId} timeline={timeline} /> : <div className="grid-empty"><span className="spinner" aria-label="Restoring your investigation" /></div>}
        </div>
        {display.data ? <Transport settings={display.data} ready={ready && Boolean(applied)} /> : null}
        {timelineOpen && display.data && range ? (
          <Timeline settings={display.data} range={range} cameraIds={shownIds} cameras={byId} timeline={timeline} loading={query.isFetching} />
        ) : null}
      </section>
    </div>
  );
}
