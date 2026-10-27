import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { frameAt, holdFor, pictureRect, trail, type TrackPoint } from "./overlay";
import { useOverlayFrames } from "./useOverlayData";
import { useClockTime, usePlaybackStore } from "./store";

const LABEL: Record<string, string> = { person: "Person", vehicle: "Vehicle" };

/**
 * Detection boxes over one tile's video, drawn from the stored detections for the shared playback clock, and the fading trail of the
 * chosen object. Boxes are placed on the picture itself, not the tile, so they stay on their objects when the video is letterboxed.
 */
export function TrackOverlay({ cameraId, video, path }: { cameraId: string; video: RefObject<HTMLVideoElement | null>; path: TrackPoint[] }) {
  const time = useClockTime();
  const tracked = usePlaybackStore(store => store.tracked);
  const { frames, withheld } = useOverlayFrames(cameraId, true);
  const hold = useMemo(() => holdFor(frames.map(frame => frame.at)), [frames]);
  const frame = frameAt(frames, time, hold);
  const wrapper = useRef<HTMLDivElement>(null);
  const [rect, setRect] = useState({ left: 0, top: 0, width: 0, height: 0 });

  useEffect(() => {
    const element = video.current;
    const tile = element?.parentElement;
    if (!element || !tile) return;
    const measure = () => setRect(pictureRect({ width: tile.clientWidth, height: tile.clientHeight }, { width: element.videoWidth, height: element.videoHeight }));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(tile);
    element.addEventListener("loadedmetadata", measure);
    element.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      element.removeEventListener("loadedmetadata", measure);
      element.removeEventListener("resize", measure);
    };
  }, [video]);

  const onThisCamera = tracked?.cameraId === cameraId;
  const segments = useMemo(() => (onThisCamera ? trail(path, time) : []), [onThisCamera, path, time]);
  const { trackObject } = usePlaybackStore.getState();

  return (
    <div ref={wrapper} className="track-overlay" data-withheld={withheld || undefined} aria-label="Detections" role="group"
         style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}>
      {segments.length ? (
        <svg className="track-trail" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true" data-track-id={tracked?.id} data-points={segments.length + 1}>
          {segments.map(segment => (
            <line key={segment.at} x1={segment.from.x} y1={segment.from.y} x2={segment.to.x} y2={segment.to.y} strokeOpacity={segment.opacity}
                  data-at={segment.at} data-x={segment.to.x.toFixed(4)} data-y={segment.to.y.toFixed(4)} />
          ))}
        </svg>
      ) : null}
      {frame?.boxes.map((item, index) => {
        const chosen = Boolean(item.track_id) && item.track_id === tracked?.id;
        const name = `${LABEL[item.label] ?? item.label}${item.track_id ? "" : " (not followed)"}`;
        return (
          <button key={`${item.track_id ?? "none"}-${index}`} type="button" className="track-box" data-chosen={chosen || undefined} data-label={item.label}
                  data-track-id={item.track_id ?? undefined} data-at={frame.at}
                  style={{ left: `${item.box.x * 100}%`, top: `${item.box.y * 100}%`, width: `${item.box.width * 100}%`, height: `${item.box.height * 100}%` }}
                  aria-label={`${name}, ${Math.round(item.confidence * 100)}% sure${chosen ? ", followed" : ""}`} aria-pressed={item.track_id ? chosen : undefined}
                  disabled={!item.track_id}
                  onClick={event => {
                    event.stopPropagation();
                    if (item.track_id) trackObject(chosen ? null : { id: item.track_id, cameraId });
                  }}
                  onDoubleClick={event => event.stopPropagation()}>
            <span className="track-box-label">{name}</span>
          </button>
        );
      })}
    </div>
  );
}
