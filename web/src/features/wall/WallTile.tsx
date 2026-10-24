import { useEffect, useRef, useState } from "react";
import type { Camera } from "@/api/types";
import { LivePlayer, type PlayerSnapshot } from "@/features/live/LivePlayer";

const PHASE_LABEL: Record<PlayerSnapshot["phase"], string> = {
  connecting: "Connecting", live: "Live", stale: "Stale", reconnecting: "Reconnecting", offline: "Offline", error: "Unavailable", paused: "Paused",
};

/** One camera on a video wall: video and its name, nothing to click. Unmounting releases the stream. */
export function WallTile({ camera, quality, highlighted }: { camera: Camera; quality: "low" | "high"; highlighted: boolean }) {
  const video = useRef<HTMLVideoElement>(null);
  const player = useRef<LivePlayer | null>(null);
  const [state, setState] = useState<PlayerSnapshot | null>(null);
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!video.current) return;
    setFallbackUrl(null);
    const instance = new LivePlayer({ cameraId: camera.id, quality, video: video.current, onChange: setState, onFallback: setFallbackUrl });
    player.current = instance;
    instance.start();
    return () => {
      instance.stop();
      player.current = null;
    };
  }, [camera.id, quality]);

  const phase = state?.phase ?? "connecting";
  return (
    <div className="tile wall-tile" data-phase={phase} data-camera-id={camera.id} data-highlighted={highlighted || undefined} role="group" aria-label={`${camera.name}: ${PHASE_LABEL[phase]}`}>
      <video ref={video} className="tile-video" muted playsInline hidden={Boolean(fallbackUrl)} />
      {fallbackUrl ? <img className="tile-video" src={fallbackUrl} alt="" onError={() => player.current?.retry()} /> : null}
      <div className="tile-top">
        <span className="tile-name" title={camera.name}>{camera.name}</span>
        <span className="tile-state" data-phase={phase}>{PHASE_LABEL[phase]}</span>
      </div>
    </div>
  );
}
