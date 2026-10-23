import { useEffect, useRef, useState } from "react";
import type { Camera } from "@/api/types";
import { LivePlayer, type PlayerSnapshot } from "@/features/live/LivePlayer";

const PHASE_LABEL: Record<PlayerSnapshot["phase"], string> = {
  connecting: "Connecting…",
  live: "Live",
  stale: "Stale: no new frames",
  reconnecting: "Reconnecting…",
  offline: "Lost",
  error: "Unavailable",
  paused: "Paused",
};

/**
 * One muted preview of the selected camera, through the same authenticated live delivery as
 * Live. It exists only while mounted: changing camera, leaving Cameras, archiving, closing and
 * signing out all unmount it, which releases the stream.
 */
export function Preview({ camera, onClose }: { camera: Camera; onClose: () => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const player = useRef<LivePlayer | null>(null);
  const [state, setState] = useState<PlayerSnapshot | null>(null);
  const [fallback, setFallback] = useState<string | null>(null);

  useEffect(() => {
    if (!video.current) return;
    const instance = new LivePlayer({ cameraId: camera.id, quality: "low", video: video.current, onChange: setState, onFallback: setFallback });
    player.current = instance;
    instance.start();
    return () => {
      instance.stop();
      player.current = null;
    };
  }, [camera.id]);

  const phase = state?.phase ?? "connecting";
  return (
    <section className="preview" aria-label={`Preview of ${camera.name}`} data-phase={phase}>
      <div className="preview-frame">
        <video ref={video} muted playsInline hidden={Boolean(fallback)} />
        {fallback ? <img src={fallback} alt="" onError={() => player.current?.retry()} /> : null}
        {phase !== "live" ? <div className="tile-overlay" role="status">{PHASE_LABEL[phase]}{state?.detail ? ` · ${state.detail}` : ""}</div> : null}
      </div>
      <div className="preview-bar">
        <span className="muted">Muted preview · {PHASE_LABEL[phase]}. Live video does not prove footage is being recorded.</span>
        {phase === "error" || phase === "offline" ? <button type="button" className="button" onClick={() => player.current?.retry()}>Retry</button> : null}
        <button type="button" className="button" onClick={onClose}>Close preview</button>
      </div>
    </section>
  );
}
