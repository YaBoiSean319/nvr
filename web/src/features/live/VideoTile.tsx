import * as Menu from "@radix-ui/react-dropdown-menu";
import { memo, useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";
import type { Camera } from "@/api/types";
import { useLenses } from "@/api/queries";
import { DewarpStage } from "@/features/dewarp/DewarpStage";
import { RAW, choiceOf, stateFor } from "@/features/dewarp/geometry";
import { LivePlayer, type PlayerSnapshot } from "./LivePlayer";
import { LOW_DATA_MAX_TILES, QualityGovernor, useLowData, useViewingPrefs, type QualityChange } from "./bandwidth";
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
  /** Low-data mode plays only the first few tiles; this one waits until it is tapped. */
  lowDataCapped: boolean;
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

function resolveQuality(override: Quality | undefined, enlarged: boolean, substream: boolean, lowData: boolean): "low" | "high" {
  if (override === "main") return "high";
  if (override === "sub") return "low";
  if (lowData && substream) return "low";
  return enlarged || !substream ? "high" : "low";
}

export const VideoTile = memo(function VideoTile({ camera, active, enlarged, suspended, lowDataCapped, onReplaceRequest }: TileProps) {
  const navigate = useNavigate();
  const video = useRef<HTMLVideoElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const player = useRef<LivePlayer | null>(null);
  const [state, setState] = useState<PlayerSnapshot | null>(null);
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);
  const override = useLiveStore(store => store.quality[camera.id]);
  const listeningId = useLiveStore(store => store.listeningId);
  const { setActive, enlarge, unenlarge: restore, remove, setQuality, setListening, move, setDewarp } = useLiveStore.getState();
  const lens = useLenses().data?.[camera.id];
  const dewarpState = useLiveStore(store => store.dewarp[camera.id]) ?? RAW;
  const [noWebgl, setNoWebgl] = useState(false);
  const onNoWebgl = useCallback(() => setNoWebgl(true), []);
  const lowData = useLowData();
  const intervalSeconds = useViewingPrefs(prefs => prefs.intervalSeconds);
  const hasSub = Boolean(camera.live_substream_available);
  const manual = override === "main" || override === "sub";
  const base = resolveQuality(override, enlarged, hasSub, lowData);
  // Auto steps a main-stream tile down to the substream when video arrives late, and back up when it recovers; a Main or Sub choice is never touched.
  const adaptive = !manual && !lowData && hasSub && base === "high";
  const governor = useRef<QualityGovernor | null>(null);
  const [adapted, setAdapted] = useState<QualityChange | null>(null);
  const quality = adaptive && adapted?.quality === "low" ? "low" : base;

  useEffect(() => {
    setAdapted(null);
    if (!adaptive) return;
    const instance = new QualityGovernor("high", { intervalMs: intervalSeconds * 1_000 }, Date.now());
    governor.current = instance;
    const timer = window.setInterval(() => {
      const change = instance.check(Date.now());
      if (change) setAdapted(change);
    }, 500);
    return () => {
      window.clearInterval(timer);
      if (governor.current === instance) governor.current = null;
    };
  }, [camera.id, adaptive, intervalSeconds]);

  // Low-data mode lets go of tiles nobody can see (scrolled away, or the page in the background) and of tiles over the cap.
  const [onScreen, setOnScreen] = useState(true);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const [tapped, setTapped] = useState(false);
  useEffect(() => {
    const element = root.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => entry && setOnScreen(entry.isIntersecting), { threshold: 0.05 });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const onChange = () => setPageVisible(!document.hidden);
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  useEffect(() => {
    if (!lowData) setTapped(false);
  }, [lowData]);
  const overCap = lowData && lowDataCapped && !tapped;
  const lowDataPaused = !suspended && lowData && (overCap || !onScreen || !pageVisible);
  const halted = suspended || lowDataPaused;

  let qualityNote: string | null = null;
  if (!manual && lowData) {
    qualityNote = overCap ? `Low-data mode: only ${LOW_DATA_MAX_TILES} tiles play; tap to play this one`
      : !onScreen || !pageVisible ? "Low-data mode: paused while out of sight"
      : hasSub ? "Low-data mode: Sub stream" : "Low-data mode: no substream, so Main stream";
  } else if (adaptive && adapted) {
    qualityNote = `Auto: ${adapted.reason}, ${adapted.quality === "low" ? "using Sub" : "back on Main"}`;
  }

  useEffect(() => {
    if (halted || !video.current) return;
    setFallbackUrl(null);
    const instance = new LivePlayer({
      cameraId: camera.id, quality, video: video.current,
      onChange: setState, onFallback: setFallbackUrl,
      onFragment: at => governor.current?.fragment(at),
      onStall: at => governor.current?.stall(at),
    });
    player.current = instance;
    instance.start();
    return () => {
      instance.stop();
      player.current = null;
    };
  }, [camera.id, quality, halted]);

  // The MJPEG fallback cannot report decoded frames, so ask the server when it last delivered one.
  useEffect(() => {
    if (!fallbackUrl || halted) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/api/cameras/${encodeURIComponent(camera.id)}/live/status`, { credentials: "same-origin" }).catch(() => null);
      const status = response?.ok ? ((await response.json()) as { state: string }) : null;
      if (status?.state === "live") player.current?.fallbackFrame();
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [fallbackUrl, camera.id, halted]);

  const listening = listeningId === camera.id;
  useEffect(() => {
    player.current?.setListening(listening);
  }, [listening]);

  // A fisheye camera shown dewarped: the video keeps playing out of sight and the stage draws it. Without WebGL the raw picture stays, with a notice.
  const dewarped = Boolean(lens) && dewarpState.mode !== "raw" && !noWebgl;
  if (dewarpState.mode !== "raw" && noWebgl && !qualityNote) qualityNote = "Dewarping needs WebGL, which this browser does not have: showing the raw picture";
  const phase = lowDataPaused ? "paused" : state?.phase ?? "connecting";
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
      data-quality={quality === "low" ? "sub" : "main"}
      data-quality-mode={manual ? "manual" : lowData ? "low-data" : "auto"}
      data-dewarp={dewarped ? dewarpState.mode : undefined}
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
      <video ref={video} className="tile-video" data-dewarped={dewarped || undefined} muted playsInline hidden={Boolean(fallbackUrl)} />
      {fallbackUrl && !halted ? <img ref={image} className="tile-video" data-dewarped={dewarped || undefined} src={fallbackUrl} alt="" onError={() => player.current?.retry()} /> : null}
      {dewarped && lens && !halted ? (
        <DewarpStage source={fallbackUrl ? image.current : video.current} lens={lens} state={dewarpState} label={camera.name} playing={live}
                     onChange={next => setDewarp(camera.id, next)} onUnsupported={onNoWebgl} />
      ) : null}
      <div className="tile-top">
        <span className="tile-name" title={camera.name}>{camera.name}</span>
        <span className="tile-state" data-phase={phase}>
          <span className="status-dot" data-tone={live ? "good" : phase === "connecting" ? undefined : phase === "stale" || phase === "reconnecting" ? "warn" : "bad"} aria-hidden="true" />
          {PHASE_LABEL[phase]}
          {state?.transport === "mjpeg" ? " · compatibility" : state?.converted ? " · converted" : state?.profile === "sub" ? " · sub" : ""}
        </span>
      </div>
      {qualityNote && !lowDataPaused ? <span className="tile-note" data-testid="tile-quality-note">{qualityNote}</span> : null}
      {!live ? (
        <div className="tile-overlay" role="status">
          {phase === "connecting" ? <span className="spinner" aria-hidden="true" /> : null}
          <span data-testid={lowDataPaused ? "tile-quality-note" : undefined}>{lowDataPaused ? qualityNote : phase === "connecting" ? "Connecting to camera…" : state?.detail ?? PHASE_LABEL[phase]}</span>
          {overCap ? <button type="button" className="button" onClick={() => setTapped(true)}>Play this tile</button> : null}
          {state?.lastFrameAt && phase !== "connecting" ? <span className="tile-last">Last frame {clock(state.lastFrameAt)}</span> : null}
          {phase === "offline" || phase === "error" ? (
            <button type="button" className="button" onClick={() => player.current?.retry()}>Retry now</button>
          ) : null}
        </div>
      ) : null}
      {/* The focused tile carries PTZ controls when its camera speaks ONVIF; the server says whether it can move. */}
      {active && camera.device_service_url && !halted ? <PtzPad cameraId={camera.id} keyTarget={root} compact /> : null}
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
        {lens ? (
          <select className="tile-quality" aria-label={`Dewarp ${camera.name}`} value={noWebgl ? "raw" : choiceOf(dewarpState)}
                  onChange={event => {
                    const value = event.target.value;
                    setDewarp(camera.id, stateFor(lens.mount, value === "raw" || value === "panorama" || value === "double_panorama" ? value : (Number(value) as 1 | 2 | 3 | 4)));
                  }}>
            <option value="raw">Raw 360°</option>
            <option value="panorama">Panorama</option>
            {lens.mount !== "wall" ? <option value="double_panorama">Double panorama</option> : null}
            <option value="1">1 view</option>
            <option value="2">2 views</option>
            <option value="3">3 views</option>
            <option value="4">4 views</option>
          </select>
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
