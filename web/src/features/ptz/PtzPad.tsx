import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type RefObject } from "react";
import { ApiError } from "@/api/client";
import { useMe } from "@/api/queries";
import { centre, getPtz, getTargets, goToPreset, move, ptzKey, releaseLease, startTracking, stop, stopTracking, takeOver, targetsKey } from "./api";

type Direction = { pan: number; tilt: number; zoom: number };

const SPEED = 0.5;
const REPEAT_MS = 1_000;
const DIRECTIONS: { key: string; label: string; symbol: string; direction: Direction }[] = [
  { key: "ArrowUp", label: "Tilt up", symbol: "▲", direction: { pan: 0, tilt: SPEED, zoom: 0 } },
  { key: "ArrowLeft", label: "Pan left", symbol: "◀", direction: { pan: -SPEED, tilt: 0, zoom: 0 } },
  { key: "ArrowRight", label: "Pan right", symbol: "▶", direction: { pan: SPEED, tilt: 0, zoom: 0 } },
  { key: "ArrowDown", label: "Tilt down", symbol: "▼", direction: { pan: 0, tilt: -SPEED, zoom: 0 } },
];
const ZOOM: { keys: string[]; label: string; symbol: string; direction: Direction }[] = [
  { keys: ["+", "="], label: "Zoom in", symbol: "+", direction: { pan: 0, tilt: 0, zoom: SPEED } },
  { keys: ["-", "_"], label: "Zoom out", symbol: "−", direction: { pan: 0, tilt: 0, zoom: -SPEED } },
];

function problem(error: unknown): string {
  return error instanceof ApiError ? error.detail : "The camera could not be moved.";
}

/**
 * Pan, tilt and zoom for one camera, by buttons (press and hold), keyboard (arrows, + and −
 * while `keyTarget` has focus) and touch, through the server under the control lease.
 */
export function PtzPad({ cameraId, keyTarget, compact = false }: {
  cameraId: string; keyTarget?: RefObject<HTMLElement | null>;
  /** Over a Live tile: compact, and click-to-centre is offered because there is a picture to click. */
  compact?: boolean;
}) {
  const client = useQueryClient();
  const me = useMe();
  const state = useQuery({ queryKey: ptzKey(cameraId), queryFn: ({ signal }) => getPtz(cameraId, signal), refetchInterval: 5_000 });
  const [error, setError] = useState<string | null>(null);
  const [centring, setCentring] = useState(false);
  const [choosing, setChoosing] = useState(false);
  const held = useRef<{ timer: number; direction: Direction } | null>(null);
  // Commands reach the camera in the order given: a quick tap's stop must never overtake its move.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const enqueue = <T,>(command: () => Promise<T>) => {
    const next = queue.current.then(command, command);
    queue.current = next.catch(() => undefined);
    return next;
  };

  const refresh = () => void client.invalidateQueries({ queryKey: ptzKey(cameraId) });
  const send = (direction: Direction) => enqueue(() => move(cameraId, direction.pan, direction.tilt, direction.zoom)).then(() => setError(null)).catch(failure => {
    setError(problem(failure));
    release(false);
    refresh();
  });
  const inFlight = useRef(false);
  function press(direction: Direction) {
    if (held.current) window.clearInterval(held.current.timer);
    const renew = () => {
      // A slow camera must not build a backlog of moves that would still run after release.
      if (inFlight.current) return;
      inFlight.current = true;
      void send(direction).finally(() => { inFlight.current = false; });
    };
    renew();
    held.current = { direction, timer: window.setInterval(renew, REPEAT_MS) };
  }
  function release(sendStop = true) {
    if (!held.current) return;
    window.clearInterval(held.current.timer);
    held.current = null;
    if (sendStop) void enqueue(() => stop(cameraId)).catch(failure => setError(problem(failure)));
  }

  // Keyboard: arrows pan and tilt, + and − zoom, while the tile (or editor) has focus.
  useEffect(() => {
    const target = keyTarget?.current;
    if (!target || !state.data?.supported) return;
    const directionFor = (event: KeyboardEvent) =>
      event.altKey || event.ctrlKey || event.metaKey ? undefined
        : DIRECTIONS.find(item => item.key === event.key)?.direction ?? ZOOM.find(item => item.keys.includes(event.key))?.direction;
    const down = (event: KeyboardEvent) => {
      // Only keys aimed at the tile itself: a focused select or button inside keeps its own keys.
      if (event.target !== target) return;
      const direction = directionFor(event);
      if (!direction) return;
      event.preventDefault();
      if (!event.repeat) press(direction);
    };
    const up = (event: KeyboardEvent) => {
      if (directionFor(event)) release();
    };
    const blur = () => release();
    target.addEventListener("keydown", down);
    target.addEventListener("keyup", up);
    target.addEventListener("blur", blur);
    return () => {
      target.removeEventListener("keydown", down);
      target.removeEventListener("keyup", up);
      target.removeEventListener("blur", blur);
    };
  }, [keyTarget, cameraId, state.data?.supported]); // eslint-disable-line react-hooks/exhaustive-deps

  // A hold never outlives the page's attention: leaving the window or tab stops the camera.
  useEffect(() => {
    const away = () => release();
    const hidden = () => { if (document.hidden) release(); };
    window.addEventListener("blur", away);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      window.removeEventListener("blur", away);
      document.removeEventListener("visibilitychange", hidden);
      release();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const data = state.data;
  // The objects in view, while choosing one to follow (they are read from what the AI worker has just stored).
  const targets = useQuery({ queryKey: targetsKey(cameraId), queryFn: ({ signal }) => getTargets(cameraId, signal), enabled: choosing, refetchInterval: 1_000 });
  if (!data) return null;
  if (!data.supported) return compact ? null : <p className="muted field-hint">{data.reason ?? "This camera cannot be moved."}</p>;
  const lease = data.lease;
  // Anyone may take the controls from tracking: manual control always wins, and ends it.
  const blocked = Boolean(lease && !lease.mine && !lease.tracking);
  const tracking = data.tracking;
  const follow = (trackId: string) => void startTracking(cameraId, trackId).then(() => { setError(null); setChoosing(false); refresh(); }).catch(failure => { setError(problem(failure)); setChoosing(false); });
  const holdProps = (direction: Direction, label: string) => ({
    type: "button" as const, className: "button icon-button ptz-button", "aria-label": label, title: label, disabled: blocked,
    onPointerDown: (event: React.PointerEvent) => { event.preventDefault(); event.currentTarget.setPointerCapture?.(event.pointerId); press(direction); },
    onPointerUp: () => release(), onPointerCancel: () => release(), onLostPointerCapture: () => release(),
    onKeyDown: (event: React.KeyboardEvent) => { if ((event.key === "Enter" || event.key === " ") && !event.repeat) { event.preventDefault(); press(direction); } },
    onKeyUp: (event: React.KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") release(); },
  });

  return (
    <>
      {choosing && !blocked && !compact ? (
        <ul className="choice-list ptz-target-list" aria-label="Objects in view">
          {(targets.data?.objects ?? []).map(object => (
            <li key={object.track_id}><button type="button" className="button" onClick={() => follow(object.track_id)}>Follow {object.label} ({Math.round(object.confidence * 100)}%)</button></li>
          ))}
          {targets.data && !targets.data.objects.length ? <li className="muted field-hint" role="status">No person or vehicle in view.</li> : null}
        </ul>
      ) : null}
      {choosing && !blocked && compact ? (
        <div className="ptz-target-layer" role="group" aria-label="Choose an object to follow">
          {(targets.data?.objects ?? []).map(object => (
            <button key={object.track_id} type="button" className="ptz-target" aria-label={`Follow ${object.label}`} data-label={object.label}
                    style={{ left: `${object.box.x * 100}%`, top: `${object.box.y * 100}%`, width: `${object.box.width * 100}%`, height: `${object.box.height * 100}%` }}
                    onClick={event => { event.stopPropagation(); follow(object.track_id); }}>
              <span>{object.label}</span>
            </button>
          ))}
          {targets.data && !targets.data.objects.length ? <p className="ptz-target-empty" role="status">No person or vehicle in view.</p> : null}
        </div>
      ) : null}
      {centring && !blocked ? (
        <div className="ptz-centre-layer" role="button" tabIndex={-1} aria-label="Centre the camera on a point"
             onClick={event => {
               const box = event.currentTarget.getBoundingClientRect();
               void centre(cameraId, (event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height)
                 .then(() => setError(null)).catch(failure => setError(problem(failure)));
             }} />
      ) : null}
      <div className="ptz-pad" data-compact={compact || undefined} role="group" aria-label="Camera movement"
           onPointerDown={event => event.stopPropagation()} onDoubleClick={event => event.stopPropagation()}>
        {lease && !lease.mine ? (
          <p className="ptz-lease" role="status">
            Controlled by {lease.holder}
            {me.data?.role === "administrator" ? (
              <button type="button" className="button" onClick={() => void takeOver(cameraId).then(refresh).catch(failure => setError(problem(failure)))}>Take over</button>
            ) : null}
          </p>
        ) : null}
        {tracking ? (
          <p className="ptz-lease ptz-following" role="status" data-state={tracking.state}>
            {tracking.state === "lost" ? `Lost the ${tracking.label}` : `Following the ${tracking.label}`}
            <button type="button" className="button" onClick={() => void stopTracking(cameraId).then(refresh).catch(failure => setError(problem(failure)))}>Stop following</button>
          </p>
        ) : null}
        {data.capabilities?.pan_tilt ? (
          <div className="ptz-arrows">
            {DIRECTIONS.map(item => <button key={item.key} {...holdProps(item.direction, item.label)} data-direction={item.key}>{item.symbol}</button>)}
          </div>
        ) : null}
        <div className="ptz-row">
          {data.capabilities?.zoom ? ZOOM.map(item => <button key={item.label} {...holdProps(item.direction, item.label)}>{item.symbol}</button>) : null}
          {compact && data.capabilities?.centre ? (
            <button type="button" className="button ptz-button" aria-pressed={centring} disabled={blocked} onClick={() => setCentring(!centring)}
                    title="Click the picture to centre the camera there">Centre on click</button>
          ) : null}
          {data.capabilities?.pan_tilt && !tracking ? (
            <button type="button" className="button ptz-button" aria-pressed={choosing} disabled={blocked} onClick={() => { setChoosing(!choosing); setCentring(false); }}
                    title="Pick a person or vehicle for the camera to follow">Follow</button>
          ) : null}
          {data.presets.length ? (
            <select className="ptz-presets" aria-label="Go to preset" value="" disabled={blocked}
                    onChange={event => event.target.value && void goToPreset(cameraId, event.target.value).then(() => setError(null)).catch(failure => setError(problem(failure)))}>
              <option value="">Go to preset…</option>
              {data.presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
            </select>
          ) : null}
          {lease?.mine ? (
            <button type="button" className="button ptz-button" onClick={() => void releaseLease(cameraId).then(refresh).catch(failure => setError(problem(failure)))} title="Let others control this camera">Release</button>
          ) : null}
        </div>
        {error ? <p className="ptz-error" role="alert">{error}</p> : null}
      </div>
    </>
  );
}
