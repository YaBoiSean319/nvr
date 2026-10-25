import { withUnmasked } from "@/features/privacy/store";
import { useEffect, useRef, useState } from "react";
import type { TimelineCamera } from "@/api/types";
import { playbackClock } from "./clock";
import { usePlaybackStore } from "./store";

type Phase = "no-footage" | "no-recording" | "loading" | "ready" | "error";

const PHASE_LABEL: Record<Phase, string | null> = {
  "no-footage": "No footage in this range",
  "no-recording": "No recording",
  loading: "Loading…",
  ready: null,
  error: "This recording could not be played",
};

interface Interval { id: number; start: number; end: number; url: string }

function intervals(camera: TimelineCamera | undefined): Interval[] {
  return (camera?.segments ?? []).map(segment => ({
    id: segment.id, start: Date.parse(segment.starts_at), end: Date.parse(segment.ends_at), url: camera ? withUnmasked(segment.media_url, camera.camera_id) : segment.media_url,
  }));
}

/**
 * One camera following the shared clock. The video is only ever corrected towards
 * the clock: it loads the interval under the playhead, seeks when it drifts and
 * shows "No recording" in gaps while the clock keeps moving.
 */
export function PlaybackTile({ id, name, camera, active, enlarged, hidden }: {
  id: string; name: string; camera: TimelineCamera | undefined; active: boolean; enlarged: boolean; hidden: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [segmentStart, setSegmentStart] = useState<number | null>(null);
  const { setActive, enlarge, unenlarge, remove } = usePlaybackStore.getState();

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    const list = intervals(camera);
    let current: Interval | null = null;

    function offsetFor(interval: Interval) {
      return Math.max(0, (playbackClock.time() - interval.start) / 1000);
    }

    function follow() {
      if (!element) return;
      if (!camera) {
        setPhase("loading");
        return;
      }
      const time = playbackClock.time();
      const interval = list.find(item => item.start <= time && time < item.end) ?? null;
      if (!interval) {
        if (current) {
          element.pause();
          element.removeAttribute("src");
          element.load();
          current = null;
          setSegmentStart(null);
        }
        setPhase(list.length ? "no-recording" : "no-footage");
        return;
      }
      if (current?.id !== interval.id) {
        current = interval;
        setSegmentStart(interval.start);
        setPhase("loading");
        element.src = interval.url;
        return; // continues in onLoaded
      }
      if (element.readyState < HTMLMediaElement.HAVE_METADATA) return;
      element.playbackRate = playbackClock.rate;
      const target = offsetFor(interval);
      const drift = Math.abs(element.currentTime - target);
      if (playbackClock.playing) {
        if (drift > Math.max(0.35, 0.3 * playbackClock.rate)) element.currentTime = target;
        if (element.paused) element.play().catch(() => undefined);
      } else {
        if (!element.paused) element.pause();
        if (drift > 0.04) element.currentTime = target;
      }
    }

    function onLoaded() {
      if (!element || !current) return;
      element.currentTime = Math.min(offsetFor(current), Math.max(0, element.duration - 0.05));
      setPhase("ready");
      follow();
    }

    function onError() {
      if (current) setPhase("error");
    }

    element.addEventListener("loadedmetadata", onLoaded);
    element.addEventListener("error", onError);
    const unsubscribe = playbackClock.subscribe(follow);
    follow();
    return () => {
      unsubscribe();
      element.removeEventListener("loadedmetadata", onLoaded);
      element.removeEventListener("error", onError);
      element.pause();
      element.removeAttribute("src");
      element.load();
    };
  }, [camera]);

  const label = PHASE_LABEL[phase];
  return (
    <div className="tile playback-tile" role="group" aria-label={`${name}: playback`} data-active={active} data-phase={phase}
         data-segment-start={segmentStart ?? undefined} data-camera-id={id} hidden={hidden}
         onClick={() => setActive(id)} onDoubleClick={() => (enlarged ? unenlarge() : enlarge(id))}>
      <video ref={video} className="tile-video" muted playsInline preload="auto" />
      <div className="tile-top">
        <span className="tile-name" title={name}>{name}</span>
      </div>
      {label ? <div className="tile-overlay" role="status" data-tone={phase === "error" ? "bad" : undefined}>{label}</div> : null}
      <div className="tile-actions" aria-label={`${name} actions`}>
        <button type="button" className="button tile-action"
                aria-label={enlarged ? `Restore grid from ${name}` : `Enlarge ${name}`}
                onClick={event => { event.stopPropagation(); if (enlarged) unenlarge(); else enlarge(id); }}>{enlarged ? "⤡" : "⤢"}</button>
        <button type="button" className="button tile-action" aria-label={`Remove ${name} from playback`}
                onClick={event => { event.stopPropagation(); remove(id); }}>✕</button>
      </div>
    </div>
  );
}
