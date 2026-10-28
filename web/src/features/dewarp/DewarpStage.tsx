import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import {
  MAX_VIEW_FOV, MIN_VIEW_FOV, clampView, drag, key as turnByKey, keyPanorama, regions, scrollPanorama, zoom,
  type DewarpState, type Lens, type Region, type VirtualView,
} from "./geometry";
import { DewarpRenderer, drawingsFor, type Source } from "./renderer";

const REGION_LABEL: Record<Region["kind"], string> = { view: "Virtual view", panorama: "Panorama", double_panorama: "Double panorama" };

interface Props {
  /** The picture to dewarp: the tile's video, or an image. */
  source: Source | null;
  lens: Lens;
  state: DewarpState;
  onChange: (state: DewarpState) => void;
  /** The camera's name, for the regions' labels. */
  label: string;
  /** Called once if the browser has no WebGL, so the tile can show the raw picture and say so. */
  onUnsupported?: () => void;
  /** Whether the picture is moving: a still source is drawn once and again when the state changes, a video every frame. */
  playing?: boolean;
}

interface Pointer { x: number; y: number }

/**
 * A fisheye picture drawn dewarped: a panorama, a double panorama, or one to four virtual views, each moved by dragging, the wheel, a pinch, or the keyboard.
 * The canvas only shows; each region is an element over it that takes the pointer and the keys, so every control is reachable without a mouse.
 */
export function DewarpStage({ source, lens, state, onChange, label, onUnsupported, playing = true }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const renderer = useRef<DewarpRenderer | null>(null);
  const latest = useRef({ source, lens, state, regionList: [] as Region[] });
  const paintNow = useRef<() => void>(() => undefined);
  const [aspect, setAspect] = useState(16 / 9);
  const [focused, setFocused] = useState(0);
  const regionList = useMemo(() => regions(state, aspect), [state, aspect]);
  latest.current = { source, lens, state, regionList };

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const created = DewarpRenderer.create(element);
    if (!created) {
      onUnsupported?.();
      return;
    }
    renderer.current = created;
    return () => {
      created.dispose();
      renderer.current = null;
    };
  }, [onUnsupported]);

  // Keep the drawing buffer the size of the tile on screen (up to twice, for sharpness), and the layout's aspect the tile's.
  useEffect(() => {
    const element = stage.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const box = entry?.contentRect;
      if (!box || !box.width || !box.height || !canvas.current) return;
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      canvas.current.width = Math.max(1, Math.round(box.width * scale));
      canvas.current.height = Math.max(1, Math.round(box.height * scale));
      setAspect(box.width / box.height);
      paintNow.current(); // Resizing the drawing buffer clears it: draw again now, whether or not the layout changes
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let frame = 0;
    let stopped = false;
    const paint = () => {
      const { source: picture, lens: currentLens, state: currentState, regionList: list } = latest.current;
      if (picture && renderer.current) renderer.current.draw(picture, currentLens, drawingsFor(currentState, list));
      if (!stopped && playing) frame = requestAnimationFrame(paint);
    };
    paintNow.current = () => {
      const { source: picture, lens: currentLens, state: currentState, regionList: list } = latest.current;
      if (picture && renderer.current) renderer.current.draw(picture, currentLens, drawingsFor(currentState, list));
    };
    paint();
    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
    };
  }, [playing, source, lens, state, regionList]);

  const mount = lens.mount;
  const setView = (index: number, view: VirtualView) => onChange({ ...state, views: state.views.map((existing, at) => (at === index ? clampView(mount, view) : existing)) });

  return (
    <div ref={stage} className="dewarp-stage" data-mode={state.mode} data-testid="dewarp-stage">
      <canvas ref={canvas} className="dewarp-canvas" width={640} height={360} />
      {regionList.map(region => (
        <RegionControls key={`${region.kind}-${region.index}`} region={region} state={state} mount={mount} focused={focused === region.index} label={label} count={regionList.length}
                        onFocus={() => setFocused(region.index)} setView={setView} onChange={onChange} />
      ))}
    </div>
  );
}

function RegionControls({ region, state, mount, focused, label, count, onFocus, setView, onChange }: {
  region: Region; state: DewarpState; mount: Lens["mount"]; focused: boolean; label: string; count: number;
  onFocus: () => void; setView: (index: number, view: VirtualView) => void; onChange: (state: DewarpState) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const pointers = useRef(new Map<number, Pointer>());
  const view = region.kind === "view" ? state.views[region.index] : undefined;
  const current = useRef({ state, view });
  current.current = { state, view };

  const apply = (next: { view?: VirtualView; pan?: number }) => {
    if (next.view) setView(region.index, next.view);
    else if (next.pan !== undefined) onChange({ ...current.current.state, pan: next.pan });
  };
  const size = () => {
    const box = element.current?.getBoundingClientRect();
    return { width: Math.max(1, box?.width ?? 1), height: Math.max(1, box?.height ?? 1) };
  };

  // A wheel turn zooms a view; it must not scroll the page, so the listener cannot be passive.
  useEffect(() => {
    const node = element.current;
    if (!node || region.kind !== "view") return;
    const onWheel = (event: WheelEvent) => {
      const { view: now } = current.current;
      if (!now) return;
      event.preventDefault();
      apply({ view: zoom(mount, now, Math.max(-3, Math.min(3, -event.deltaY / 100))) });
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [region.kind, mount]); // eslint-disable-line react-hooks/exhaustive-deps

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    onFocus();
    element.current?.focus({ preventScroll: true });
    try {
      element.current?.setPointerCapture?.(event.pointerId);
    } catch {
      // A pointer the browser is not tracking (a synthetic one) cannot be captured: its moves still reach this element.
    }
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const before = pointers.current.get(event.pointerId);
    if (!before) return;
    const { width, height } = size();
    const { state: now, view: nowView } = current.current;
    if (pointers.current.size >= 2 && nowView) {
      // Two fingers: the spread zooms (the distance between them against what it was).
      const others = [...pointers.current.entries()].filter(([id]) => id !== event.pointerId).map(([, point]) => point);
      const other = others[0]!;
      const oldDistance = Math.hypot(before.x - other.x, before.y - other.y);
      const newDistance = Math.hypot(event.clientX - other.x, event.clientY - other.y);
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (oldDistance > 4 && newDistance > 4) apply({ view: clampView(mount, { ...nowView, fov: Math.min(MAX_VIEW_FOV, Math.max(MIN_VIEW_FOV, nowView.fov * (oldDistance / newDistance))) }) });
      return;
    }
    const dx = (event.clientX - before.x) / width;
    const dy = (event.clientY - before.y) / height;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (nowView) apply({ view: drag(mount, nowView, dx, dy, width / height) });
    else if (region.kind !== "view") apply({ pan: scrollPanorama(mount, region.kind, now.pan, dx) });
  }

  function onPointerEnd(event: ReactPointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId);
    try {
      element.current?.releasePointerCapture?.(event.pointerId);
    } catch {
      // Nothing was captured.
    }
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const { state: now, view: nowView } = current.current;
    if (nowView) {
      const next = turnByKey(mount, nowView, event.key, event.shiftKey);
      if (next) {
        event.preventDefault();
        event.stopPropagation();
        apply({ view: next });
      }
    } else if (region.kind !== "view") {
      const pan = keyPanorama(mount, region.kind, now.pan, event.key, event.shiftKey);
      if (pan !== null) {
        event.preventDefault();
        event.stopPropagation();
        apply({ pan });
      }
    }
  }

  const name = region.kind === "view" ? `${REGION_LABEL.view} ${region.index + 1} of ${count}` : REGION_LABEL[region.kind];
  return (
    <div ref={element} className="dewarp-region" role="group" tabIndex={0} data-focused={focused} data-kind={region.kind} data-index={region.index}
         data-pan={view ? view.pan.toFixed(1) : state.pan.toFixed(1)} data-tilt={view?.tilt.toFixed(1)} data-fov={view?.fov.toFixed(1)}
         aria-label={`${label}: ${name}`}
         aria-description={region.kind === "view" ? "Drag, or use the arrow keys, to turn the view; scroll, pinch, plus and minus zoom." : "Drag, or use the left and right arrow keys, to scroll the panorama."}
         style={{ left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.width * 100}%`, height: `${region.height * 100}%` }}
         onFocus={onFocus} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerEnd} onPointerCancel={onPointerEnd} onKeyDown={onKeyDown}
         onDoubleClick={event => event.stopPropagation()}>
      {view ? <span className="dewarp-region-label">{region.index + 1}</span> : null}
    </div>
  );
}
