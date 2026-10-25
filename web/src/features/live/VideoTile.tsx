import * as Menu from "@radix-ui/react-dropdown-menu";
import { memo, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";
import type { Camera } from "@/api/types";
import { LivePlayer, type PlayerSnapshot } from "./LivePlayer";
import { CAMERA_DRAG_TYPE } from "@/features/cameras/CameraBrowser";
import { usePlaybackStore } from "@/features/playback/store";
import { PtzPad } from "@/features/ptz/PtzPad";
import { useLiveStore, type Quality } from "./store";

interface TileProps {
  camera: Camera;
  active: boolean;
  enlarged: boolean;
  /** Hidden behind an enlarged tile: release the stream but keep membership. */
  suspended: boolean;
  onReplaceRequest: () => void;
}

const PHASE_LABEL: Record<PlayerSnapshot["phase"], string> = {
  connecting: "Connecting",
  live: "Live",
  stale: "Stale",
  reconnecting: "Reconnecting",
  offline: "Offline",
  error: "Unavailable",
  paused: "Paused",
};

function clock(timestamp: number | null): string {
  return timestamp ? new Date(timestamp).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "";
}

function resolveQuality(override: Quality | undefined, enlarged: boolean, substream: boolean): "low" | "high" {
  if (override === "main") return "high";
  if (override === "sub") return "low";
  return enlarged || !substream ? "high" : "low";
}

export const VideoTile = memo(function VideoTile({ camera, active, enlarged, suspended, onReplaceRequest }: TileProps) {
  const navigate = useNavigate();
  const video = useRef<HTMLVideoElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const player = useRef<LivePlayer | null>(null);
  const [state, setState] = useState<PlayerSnapshot | null>(null);
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);
  const override = useLiveStore(store => store.quality[camera.id]);
  const listeningId = useLiveStore(store => store.listeningId);
  const { setActive, enlarge, unenlarge: restore, remove, setQuality, setListening, move } = useLiveStore.getState();
  const quality = resolveQuality(override, enlarged, Boolean(camera.live_substream_available));

  useEffect(() => {
    if (suspended || !video.current) return;
    setFallbackUrl(null);
    const instance = new LivePlayer({
      cameraId: camera.id, quality, video: video.current,
      onChange: setState, onFallback: setFallbackUrl,
    });
    player.current = instance;
    instance.start();
    return () => {
      instance.stop();
      player.current = null;
    };
  }, [camera.id, quality, suspended]);

  // The MJPEG fallback cannot report decoded frames, so ask the server when it last delivered one.
  useEffect(() => {
    if (!fallbackUrl || suspended) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/cameras/${encodeURIComponent(camera.id)}/live/status`, { credentials: "same-origin" }).catch(() => null);
      const status = response?.ok ? ((await response.json()) as { state: string }) : null;
      if (status?.state === "live") player.current?.fallbackFrame();
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [fallbackUrl, camera.id, suspended]);

  const listening = listeningId === camera.id;
  useEffect(() => {
    player.current?.setListening(listening);
  }, [listening]);

  const phase = state?.phase ?? "connecting";
  const live = phase === "live";
  const audioAvailable = Boolean(state?.audioAvailable && camera.audio_enabled);

  function snapshot() {
    const source = fallbackUrl ? image.current : video.current;
    const width = source instanceof HTMLVideoElement ? source.videoWidth : source?.naturalWidth;
    const height = source instanceof HTMLVideoElement ? source.videoHeight : source?.naturalHeight;
    if (!source || !width || !height) return;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d")?.drawImage(source, 0, 0);
    const taken = state?.lastFrameAt ?? Date.now();
    canvas.toBlob(blob => {
      if (!blob) return;
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${camera.name.replace(/[^a-z0-9_-]+/gi, "-")}-${new Date(taken).toISOString().replace(/[:.]/g, "-")}.png`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1_000);
    }, "image/png");
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter") enlarged ? restore() : enlarge(camera.id);
    if (event.key === "Delete" || event.key === "Backspace") remove(camera.id);
    if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
      event.preventDefault();
      move(camera.id, event.key === "ArrowLeft" ? -1 : 1);
    }
  }

  return (
    <div
      ref={root}
      className="tile"
      data-active={active}
      data-phase={phase}
      data-camera-id={camera.id}
      data-transport={state?.transport ?? undefined}
      tabIndex={0}
      role="group"
      aria-label={`${camera.name}: ${PHASE_LABEL[phase]}`}
      onFocus={() => setActive(camera.id)}
      onPointerDown={() => setActive(camera.id)}
      onDoubleClick={() => (enlarged ? restore() : enlarge(camera.id))}
      onKeyDown={onKeyDown}
      draggable={!enlarged}
      onDragStart={event => {
        event.dataTransfer.setData(CAMERA_DRAG_TYPE, camera.id);
        event.dataTransfer.effectAllowed = "move";
      }}
    >
      <video ref={video} className="tile-video" muted playsInline hidden={Boolean(fallbackUrl)} />
      {fallbackUrl && !suspended ? <img ref={image} className="tile-video" src={fallbackUrl} alt="" onError={() => player.current?.retry()} /> : null}
      <div className="tile-top">
        <span className="tile-name" title={camera.name}>{camera.name}</span>
        <span className="tile-state" data-phase={phase}>
          <span className="status-dot" data-tone={live ? "good" : phase === "connecting" ? undefined : phase === "stale" || phase === "reconnecting" ? "warn" : "bad"} aria-hidden="true" />
          {PHASE_LABEL[phase]}
          {state?.transport === "mjpeg" ? " · compatibility" : state?.profile === "sub" ? " · sub" : ""}
        </span>
      </div>
      {!live ? (
        <div className="tile-overlay" role="status">
          {phase === "connecting" ? <span className="spinner" aria-hidden="true" /> : null}
          <span>{phase === "connecting" ? "Connecting to camera…" : state?.detail ?? PHASE_LABEL[phase]}</span>
          {state?.lastFrameAt && phase !== "connecting" ? <span className="tile-last">Last frame {clock(state.lastFrameAt)}</span> : null}
          {phase === "offline" || phase === "error" ? (
            <button type="button" className="button" onClick={() => player.current?.retry()}>Retry now</button>
          ) : null}
        </div>
      ) : null}
      {/* The focused tile carries PTZ controls when its camera speaks ONVIF; the server says whether it can move. */}
      {active && camera.device_service_url && !suspended ? <PtzPad cameraId={camera.id} keyTarget={root} compact /> : null}
      <div className="tile-actions" aria-label={`${camera.name} actions`}>
        <button type="button" className="button icon-button tile-action" onClick={() => (enlarged ? restore() : enlarge(camera.id))} aria-label={enlarged ? `Restore grid` : `Enlarge ${camera.name}`} title={enlarged ? "Restore grid (Esc)" : "Enlarge"}>
          {enlarged ? "⤡" : "⤢"}
        </button>
        <button type="button" className="button icon-button tile-action" onClick={snapshot} disabled={!state?.lastFrameAt} aria-label={`Snapshot of ${camera.name}`} title="Snapshot">◉</button>
        {audioAvailable ? (
          <button type="button" className="button icon-button tile-action" aria-pressed={listening} onClick={() => setListening(listening ? null : camera.id)}
                  aria-label={listening ? `Mute ${camera.name}` : `Listen to ${camera.name}`} title={listening ? "Mute" : "Listen"}>
            {listening ? "🔊" : "🔈"}
          </button>
        ) : null}
        <select className="tile-quality" aria-label={`Video quality for ${camera.name}`} value={override ?? "auto"} onChange={event => setQuality(camera.id, event.target.value as Quality)}>
          <option value="auto">Auto</option>
          <option value="main">Main</option>
          <option value="sub" disabled={!camera.live_substream_available}>Sub{camera.live_substream_available ? "" : " (none)"}</option>
        </select>
        <Menu.Root>
          <Menu.Trigger className="button icon-button tile-action" aria-label={`More actions for ${camera.name}`} title="More">⋯</Menu.Trigger>
          <Menu.Portal>
            <Menu.Content className="menu" align="end" sideOffset={4}>
              <Menu.Item className="menu-item" onSelect={() => move(camera.id, -1)}>Move earlier</Menu.Item>
              <Menu.Item className="menu-item" onSelect={() => move(camera.id, 1)}>Move later</Menu.Item>
              <Menu.Item className="menu-item" onSelect={onReplaceRequest}>Replace with…</Menu.Item>
              <Menu.Separator className="menu-separator" />
              <Menu.Item className="menu-item" onSelect={() => { usePlaybackStore.getState().openInPlayback(camera.id); navigate("/playback"); }}>
                Open in Playback
              </Menu.Item>
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
        <button type="button" className="button icon-button tile-action" onClick={() => remove(camera.id)} aria-label={`Remove ${camera.name} from view`} title="Remove">✕</button>
      </div>
    </div>
  );
});
