import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { withUnmasked } from "@/features/privacy/store";
import { playbackClock } from "./clock";
import type { OverlayFrame, TrackPoint } from "./overlay";

interface FrameResponse { frames: { at: string; boxes: OverlayFrame["boxes"] }[]; withheld: number; truncated: boolean }
interface TrackResponse { track_id: string; camera_id: string; label: string; points: { at: string; box: TrackPoint["box"] }[]; withheld: number }

const CHUNK_MS = 60_000;
const BEHIND_MS = 12_000; // A frame is held on screen for a few seconds, so look a little behind the playhead
const AHEAD_MS = 40_000;
const RETRY_MS = 10_000;

/**
 * The stored detections around the playback clock for one camera, fetched a minute at a time as the clock nears them. Nothing is fetched
 * while the overlay is off.
 */
export function useOverlayFrames(cameraId: string, enabled: boolean): { frames: OverlayFrame[]; withheld: number } {
  const chunks = useRef(new Map<number, { frames: OverlayFrame[]; withheld: number }>());
  const inflight = useRef(new Set<number>());
  const failed = useRef(new Map<number, number>());
  const [version, setVersion] = useState(0);

  useEffect(() => {
    chunks.current = new Map();
    inflight.current = new Set();
    failed.current = new Map();
    setVersion(count => count + 1);
    if (!enabled) return;
    let cancelled = false;
    const controller = new AbortController();

    function want() {
      const time = playbackClock.time();
      const first = Math.floor((time - BEHIND_MS) / CHUNK_MS);
      const last = Math.floor((time + AHEAD_MS) / CHUNK_MS);
      for (let chunk = first; chunk <= last; chunk += 1) {
        if (chunks.current.has(chunk) || inflight.current.has(chunk) || (failed.current.get(chunk) ?? 0) > Date.now()) continue;
        inflight.current.add(chunk);
        const params = new URLSearchParams({ camera_id: cameraId, start: new Date(chunk * CHUNK_MS).toISOString(), end: new Date((chunk + 1) * CHUNK_MS).toISOString() });
        api<FrameResponse>(withUnmasked(`/api/playback/overlay?${params}`, cameraId), { signal: controller.signal })
          .then(response => {
            if (cancelled) return;
            chunks.current.set(chunk, { frames: response.frames.map(frame => ({ at: Date.parse(frame.at), boxes: frame.boxes })), withheld: response.withheld });
            setVersion(count => count + 1);
          })
          .catch(() => {
            if (!cancelled) failed.current.set(chunk, Date.now() + RETRY_MS);
          })
          .finally(() => inflight.current.delete(chunk));
      }
    }

    want();
    const unsubscribe = playbackClock.subscribe(want);
    return () => {
      cancelled = true;
      controller.abort();
      unsubscribe();
    };
  }, [cameraId, enabled]);

  void version;
  const ordered = [...chunks.current.entries()].sort((a, b) => a[0] - b[0]);
  return { frames: ordered.flatMap(([, chunk]) => chunk.frames), withheld: ordered.reduce((sum, [, chunk]) => sum + chunk.withheld, 0) };
}

/** The whole stored path of one tracked object (empty until it arrives, and when nothing is tracked). Shared by every caller through the query cache. */
export function useTrackPath(trackId: string | null, cameraId: string | null): { points: TrackPoint[]; loading: boolean; error: boolean } {
  const query = useQuery({
    queryKey: ["overlay-track", trackId],
    enabled: Boolean(trackId && cameraId),
    staleTime: 60_000,
    retry: false,
    queryFn: ({ signal }) => api<TrackResponse>(withUnmasked(`/api/playback/overlay/tracks/${encodeURIComponent(trackId!)}`, cameraId!), { signal }),
  });
  const points = useMemo(() => (query.data ? query.data.points.map(point => ({ at: Date.parse(point.at), box: point.box })) : []), [query.data]);
  return { points, loading: Boolean(trackId) && query.isLoading, error: query.isError };
}
