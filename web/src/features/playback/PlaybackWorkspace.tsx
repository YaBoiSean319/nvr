import { useEffect, useMemo, useState } from "react";
import { ApiError } from "@/api/client";
import { useCameras, useDisplaySettings, useMe, usePlaybackTimeline } from "@/api/queries";
import type { TimelineCamera } from "@/api/types";
import { BrowserFrame, CameraList } from "@/features/cameras/CameraBrowser";
import { MAX_DISPLAYED_CAMERAS } from "@/lib/layout";
import { toLocalInput } from "@/lib/zonedTime";
import { playbackClock } from "./clock";
import { AttributeSearch } from "./AttributeSearch";
import { ExportDrawer } from "./ExportDrawer";
import { FullChooser } from "./FullChooser";
import { MarkControls } from "./MarkControls";
import { OverlayControls } from "./OverlayControls";
import { Investigation } from "./Investigation";
import { PlaybackGrid } from "./PlaybackGrid";
import { SearchBar } from "./SearchBar";
import { savePlaybackState, usePlaybackStore } from "./store";
import { Timeline } from "./Timeline";
import { Transport } from "./Transport";

const SAVE_DELAY_MS = 750;
// An alarm opens the minutes either side of its moment.
const ALARM_CONTEXT_MS = 5 * 60_000;

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
  const pendingOpen = usePlaybackStore(store => store.pendingOpen);
  const exportOpen = usePlaybackStore(store => store.exportOpen);
  const attributeSearchOpen = usePlaybackStore(store => store.attributeSearchOpen);
  const { toggle, setSidebarOpen, clearNotice, searchLoaded } = usePlaybackStore.getState();
  const [incoming, setIncoming] = useState<string | null>(null);
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

  // Open in Playback from Live: join the current investigation, or start a past-hour one. From an
  // alarm: search the minutes around its moment and wait there, paused, with the camera selected.
  useEffect(() => {
    if (!ready || !pendingOpen || !display.data) return;
    const store = usePlaybackStore.getState();
    const { cameraId, at } = pendingOpen;
    store.clearPendingOpen();
    if (!byId.has(cameraId)) return;
    const shown = store.cameraIds.filter(id => byId.has(id));
    if (shown.includes(cameraId)) store.setActive(cameraId);
    else if (shown.length >= MAX_DISPLAYED_CAMERAS) setIncoming(cameraId); // ask before replacing anything
    else store.toggle(cameraId);
    const zone = display.data.time_zone;
    const search = (start: number, end: number) => {
      const draft = { start: toLocalInput(start, zone), end: toLocalInput(end, zone), startFold: "", endFold: "" };
      store.setDraft(draft);
      store.apply({ start: new Date(start).toISOString(), end: new Date(end).toISOString(), draft });
    };
    if (at !== null) {
      search(at - ALARM_CONTEXT_MS, at + ALARM_CONTEXT_MS);
      playbackClock.seek(at);
    } else if (!store.applied) {
      const now = Date.now();
      search(now - 3_600_000, now);
    }
  }, [ready, pendingOpen, byId, display.data]);

  // Leaving Playback stops the clock; the position is remembered.
  useEffect(() => () => playbackClock.pause(), []);

  // A new search starts paused at the earliest recording among the searched cameras.
  useEffect(() => {
    const data = query.data;
    if (!seekToEarliest || !applied || !data || query.isPlaceholderData) return;
    if (Date.parse(data.start) !== Date.parse(applied.start) || Date.parse(data.end) !== Date.parse(applied.end)) return;
    searchLoaded();
    // Someone who already moved the playhead or pressed Play keeps their choice.
    if (playbackClock.playing || playbackClock.time() !== Date.parse(applied.start)) return;
    const starts = data.cameras.flatMap(row => row.segments.map(segment => Math.max(Date.parse(segment.starts_at), Date.parse(applied.start))));
    playbackClock.seek(starts.length ? Math.min(...starts) : Date.parse(applied.start));
  }, [query.data, query.isPlaceholderData, applied, seekToEarliest, searchLoaded]);

  const camerasError = cameras.error instanceof ApiError ? cameras.error.detail : cameras.error ? "Cameras could not be loaded." : null;
  const timelineError = query.error instanceof ApiError ? query.error.detail : query.error ? "The timeline could not be loaded." : null;
  const range = useMemo(() => (applied ? { start: Date.parse(applied.start), end: Date.parse(applied.end) } : null), [applied]);
  const names = useMemo(() => new Map([...byId].map(([id, camera]) => [id, camera.name])), [byId]);

  return (
    <div className="workspace playback-workspace" data-sidebar={sidebarOpen ? "open" : "closed"} data-export={(exportOpen && range) || attributeSearchOpen ? "open" : "closed"}>
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
          <button type="button" className="button" aria-expanded={attributeSearchOpen} aria-controls="attribute-search"
                  onClick={() => usePlaybackStore.getState().setAttributeSearchOpen(!attributeSearchOpen)}>Find by attributes…</button>
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
        {display.data ? (
          <Transport settings={display.data} ready={ready && Boolean(applied)}>
            <Investigation settings={display.data} range={range} names={names}
                           rows={shownIds.map(id => timeline.get(id)).filter((row): row is TimelineCamera => row !== undefined)} />
            <OverlayControls ready={ready && Boolean(applied)} />
            <MarkControls ready={ready && Boolean(applied)} />
          </Transport>
        ) : null}
        {timelineOpen && display.data && range ? (
          <Timeline settings={display.data} range={range} cameraIds={shownIds} cameras={byId} timeline={timeline} loading={query.isFetching} />
        ) : null}
      </section>
      {exportOpen && range && display.data ? (
        <ExportDrawer settings={display.data} range={range} shownIds={shownIds} cameras={byId} timeline={timeline} />
      ) : null}
      {attributeSearchOpen && display.data ? <AttributeSearch settings={display.data} range={range} shownIds={shownIds} /> : null}
      <FullChooser incoming={incoming ? byId.get(incoming) : undefined} current={shownIds.map(id => byId.get(id)!)}
                   onClose={() => setIncoming(null)}
                   onReplace={target => { if (incoming) usePlaybackStore.getState().replaceCamera(target, incoming); setIncoming(null); }} />
    </div>
  );
}
