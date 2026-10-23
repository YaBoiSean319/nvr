import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { Zone } from "./api";
import { latestSegment } from "./api";

const MAX_ZONES = 16;
const MIN_SIZE = 0.02;
const round = (value: number) => Math.round(value * 10000) / 10000;
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/** Keep a rectangle inside the frame with a minimum size, rounded to stable precision. */
export function normalize(zone: Zone): Zone {
  const width = clamp(zone.width, MIN_SIZE, 1);
  const height = clamp(zone.height, MIN_SIZE, 1);
  return { x: round(clamp(zone.x, 0, 1 - width)), y: round(clamp(zone.y, 0, 1 - height)), width: round(width), height: round(height) };
}

type Drag = { kind: "draw" | "move" | "resize"; index: number; startX: number; startY: number; origin: Zone };

/**
 * Normalized rectangles over a representative recorded frame. Coordinates are fractions of the
 * frame, so a missing frame or a resolution change never moves a zone. Draw on empty space,
 * drag a zone to move it, drag its corner to resize; arrows move and Alt+arrows resize the
 * selected zone, and every value can be typed.
 */
export function ZoneEditor({ cameraId, zones, onChange, label, readOnly = false }: {
  cameraId: string; zones: Zone[]; onChange: (zones: Zone[]) => void; label: string; readOnly?: boolean;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [frame, setFrame] = useState<{ url: string; ratio: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    latestSegment(cameraId, controller.signal).then(segment => {
      if (!segment || controller.signal.aborted) return;
      const url = `/api/cameras/${encodeURIComponent(cameraId)}/segments/${segment.id}/snapshot?at=${encodeURIComponent(segment.starts_at)}`;
      const image = new Image();
      image.onload = () => !controller.signal.aborted && image.naturalWidth && setFrame({ url, ratio: `${image.naturalWidth} / ${image.naturalHeight}` });
      image.src = url;
    }).catch(() => undefined);
    return () => controller.abort();
  }, [cameraId]);

  const point = (event: PointerEvent) => {
    const rect = stage.current!.getBoundingClientRect();
    return { x: clamp((event.clientX - rect.left) / rect.width, 0, 1), y: clamp((event.clientY - rect.top) / rect.height, 0, 1) };
  };

  function update(index: number, zone: Zone) {
    onChange(zones.map((existing, position) => (position === index ? normalize(zone) : existing)));
  }

  function onStageDown(event: PointerEvent<HTMLDivElement>) {
    if (readOnly || event.button !== 0 || zones.length >= MAX_ZONES || event.target !== stage.current) return;
    const at = point(event);
    const zone = { x: at.x, y: at.y, width: MIN_SIZE, height: MIN_SIZE };
    onChange([...zones, normalize(zone)]);
    setSelected(zones.length);
    drag.current = { kind: "draw", index: zones.length, startX: at.x, startY: at.y, origin: zone };
    stage.current!.setPointerCapture(event.pointerId);
  }

  function onZoneDown(event: PointerEvent<HTMLElement>, index: number, kind: "move" | "resize") {
    if (readOnly || event.button !== 0) return;
    event.stopPropagation();
    const at = point(event);
    setSelected(index);
    drag.current = { kind, index, startX: at.x, startY: at.y, origin: zones[index]! };
    stage.current!.setPointerCapture(event.pointerId);
  }

  function onMove(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    const at = point(event);
    const dx = at.x - current.startX, dy = at.y - current.startY;
    if (current.kind === "move") update(current.index, { ...current.origin, x: current.origin.x + dx, y: current.origin.y + dy });
    else if (current.kind === "resize") update(current.index, { ...current.origin, width: current.origin.width + dx, height: current.origin.height + dy });
    else update(current.index, { x: Math.min(at.x, current.startX), y: Math.min(at.y, current.startY), width: Math.abs(dx), height: Math.abs(dy) });
  }

  function onUp() {
    const current = drag.current;
    drag.current = null;
    // A click without a drag on empty frame space does not leave a sliver behind.
    const drawn = current?.kind === "draw" ? zones[current.index] : undefined;
    if (drawn && drawn.width <= MIN_SIZE && drawn.height <= MIN_SIZE) remove(current!.index);
  }

  function onZoneKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (readOnly) return;
    const zone = zones[index]!;
    const step = event.shiftKey ? 0.05 : 0.01;
    const delta: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (event.key === "Delete" || event.key === "Backspace") {
      remove(index);
    } else if (event.key in delta) {
      const [dx, dy] = delta[event.key]!;
      update(index, event.altKey ? { ...zone, width: zone.width + dx, height: zone.height + dy } : { ...zone, x: zone.x + dx, y: zone.y + dy });
    } else return;
    event.preventDefault();
  }

  function remove(index: number) {
    onChange(zones.filter((_, position) => position !== index));
    setSelected(null);
  }

  function add() {
    onChange([...zones, normalize({ x: 0.25, y: 0.25, width: 0.5, height: 0.5 })]);
    setSelected(zones.length);
  }

  const percent = (value: number) => Math.round(value * 1000) / 10;
  return (
    <div className="zone-editor" role="group" aria-label={label}>
      <div ref={stage} className="zone-stage" data-frame={frame ? "loaded" : "none"}
           style={{ aspectRatio: frame?.ratio ?? "16 / 9", backgroundImage: frame ? `url("${frame.url}")` : undefined }}
           onPointerDown={onStageDown} onPointerMove={onMove} onPointerUp={onUp}>
        {!frame ? <span className="zone-placeholder">No recorded frame yet. Zones use fractions of the frame, so they stay put when one appears.</span> : null}
        {zones.map((zone, index) => (
          <button key={index} type="button" className="zone" data-selected={selected === index}
                  aria-label={`Zone ${index + 1}: ${percent(zone.x)}% from left, ${percent(zone.y)}% from top, ${percent(zone.width)}% wide, ${percent(zone.height)}% tall`}
                  style={{ left: `${zone.x * 100}%`, top: `${zone.y * 100}%`, width: `${zone.width * 100}%`, height: `${zone.height * 100}%` }}
                  onPointerDown={event => onZoneDown(event, index, "move")} onFocus={() => setSelected(index)} onKeyDown={event => onZoneKey(event, index)}>
            <span className="zone-number" aria-hidden="true">{index + 1}</span>
            {!readOnly ? <span className="zone-handle" aria-hidden="true" onPointerDown={event => onZoneDown(event, index, "resize")} /> : null}
          </button>
        ))}
      </div>
      <p className="muted field-hint">
        {zones.length ? `${zones.length} zone${zones.length === 1 ? "" : "s"}.` : "No zones: the whole frame is used."}
        {!readOnly ? " Drag on the frame to draw. Select a zone and use arrows to move, Alt+arrows to resize, Delete to remove." : ""}
      </p>
      {zones.length ? (
        <table className="zone-table">
          <thead><tr><th scope="col">Zone</th><th scope="col">Left %</th><th scope="col">Top %</th><th scope="col">Width %</th><th scope="col">Height %</th>{!readOnly ? <th scope="col"><span className="visually-hidden">Actions</span></th> : null}</tr></thead>
          <tbody>
            {zones.map((zone, index) => (
              <tr key={index} data-selected={selected === index}>
                <th scope="row">{index + 1}</th>
                {(["x", "y", "width", "height"] as const).map(field => (
                  <td key={field}>
                    <input type="number" min={0} max={100} step={0.1} value={percent(zone[field])} disabled={readOnly}
                           aria-label={`Zone ${index + 1} ${field === "x" ? "left" : field === "y" ? "top" : field} percent`}
                           onChange={event => event.target.value !== "" && update(index, { ...zone, [field]: Number(event.target.value) / 100 })} />
                  </td>
                ))}
                {!readOnly ? <td><button type="button" className="button" data-variant="ghost" onClick={() => remove(index)} aria-label={`Remove zone ${index + 1}`}>Remove</button></td> : null}
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {!readOnly ? <button type="button" className="button" onClick={add} disabled={zones.length >= MAX_ZONES}>Add zone</button> : null}
    </div>
  );
}
