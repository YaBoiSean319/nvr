import { useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useRepresentativeFrame } from "./ZoneEditor";

export interface Line { x1: number; y1: number; x2: number; y2: number }
export type CrossingDirection = "both" | "forward" | "reverse";

const MIN_LENGTH = 0.02;
const round = (value: number) => Math.round(value * 10000) / 10000;
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));
const length = (line: Line) => Math.hypot(line.x2 - line.x1, line.y2 - line.y1);

/** Keep both ends inside the frame, rounded to stable precision. */
export function normalizeLine(line: Line): Line {
  return { x1: round(clamp(line.x1, 0, 1)), y1: round(clamp(line.y1, 0, 1)), x2: round(clamp(line.x2, 0, 1)), y2: round(clamp(line.y2, 0, 1)) };
}

export const DIRECTION_LABEL: Record<CrossingDirection, string> = {
  both: "Either direction",
  forward: "Forward (with the arrow)",
  reverse: "Reverse (against the arrow)",
};

type Drag = { end: "a" | "b" } | { end: "draw"; startX: number; startY: number };
const ratioOf = (ratio: string | undefined) => {
  const [width, height] = (ratio ?? "16 / 9").split("/").map(part => Number(part.trim()));
  return width && height ? width / height : 16 / 9;
};

/**
 * One line over a representative recorded frame, from end A to end B. Crossing it counts in
 * the arrow's direction ("forward"), against it ("reverse") or either way. Draw by dragging on
 * the frame, drag either end to move it, or focus an end and use arrows (Shift for larger steps);
 * every coordinate can also be typed.
 */
export function LineEditor({ cameraId, line, direction, onChange, label, readOnly = false }: {
  cameraId: string | null; line: Line; direction: CrossingDirection; onChange: (line: Line) => void; label: string; readOnly?: boolean;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [selected, setSelected] = useState<"a" | "b" | null>(null);
  const frame = useRepresentativeFrame(cameraId);
  const aspect = ratioOf(frame?.ratio);

  const point = (event: PointerEvent) => {
    const rect = stage.current!.getBoundingClientRect();
    return { x: clamp((event.clientX - rect.left) / rect.width, 0, 1), y: clamp((event.clientY - rect.top) / rect.height, 0, 1) };
  };
  const update = (next: Line) => onChange(normalizeLine(next));

  function onStageDown(event: PointerEvent<HTMLDivElement>) {
    if (readOnly || event.button !== 0 || event.target !== stage.current) return;
    const at = point(event);
    drag.current = { end: "draw", startX: at.x, startY: at.y };
    stage.current!.setPointerCapture(event.pointerId);
  }

  function onEndDown(event: PointerEvent<HTMLElement>, end: "a" | "b") {
    if (readOnly || event.button !== 0) return;
    event.stopPropagation();
    setSelected(end);
    drag.current = { end };
    stage.current!.setPointerCapture(event.pointerId);
  }

  function onMove(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    const at = point(event);
    if (current.end === "draw") {
      if (Math.hypot(at.x - current.startX, at.y - current.startY) >= MIN_LENGTH) update({ x1: current.startX, y1: current.startY, x2: at.x, y2: at.y });
    } else update(current.end === "a" ? { ...line, x1: at.x, y1: at.y } : { ...line, x2: at.x, y2: at.y });
  }

  function onEndKey(event: KeyboardEvent<HTMLButtonElement>, end: "a" | "b") {
    if (readOnly) return;
    const step = event.shiftKey ? 0.05 : 0.01;
    const delta: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (!(event.key in delta)) return;
    const [dx, dy] = delta[event.key]!;
    update(end === "a" ? { ...line, x1: line.x1 + dx, y1: line.y1 + dy } : { ...line, x2: line.x2 + dx, y2: line.y2 + dy });
    event.preventDefault();
  }

  // Drawn in a viewBox with the frame's proportions, so the arrow is truly perpendicular.
  const width = 1000 * aspect, height = 1000;
  const ax = line.x1 * width, ay = line.y1 * height, bx = line.x2 * width, by = line.y2 * height;
  const pixelLength = Math.hypot(bx - ax, by - ay) || 1;
  // Forward is to the right of A→B on screen; the server tests the same side.
  const nx = (by - ay) / pixelLength, ny = -(bx - ax) / pixelLength;
  const mx = (ax + bx) / 2, my = (ay + by) / 2;
  const arrow = (sign: number) => {
    const tipX = mx + nx * 90 * sign, tipY = my + ny * 90 * sign;
    const baseX = mx + nx * 50 * sign, baseY = my + ny * 50 * sign;
    const px = -ny * 22, py = nx * 22;
    return (
      <g key={sign}>
        <line x1={mx} y1={my} x2={baseX} y2={baseY} className="line-arrow" />
        <polygon points={`${tipX},${tipY} ${baseX + px},${baseY + py} ${baseX - px},${baseY - py}`} className="line-arrowhead" />
      </g>
    );
  };
  const percent = (value: number) => Math.round(value * 1000) / 10;
  const short = length(line) < MIN_LENGTH;

  return (
    <div className="zone-editor line-editor" role="group" aria-label={label}>
      <div ref={stage} className="zone-stage" data-frame={frame ? "loaded" : "none"}
           style={{ aspectRatio: frame?.ratio ?? "16 / 9", backgroundImage: frame ? `url("${frame.url}")` : undefined }}
           onPointerDown={onStageDown} onPointerMove={onMove} onPointerUp={() => { drag.current = null; }}>
        {!frame ? <span className="zone-placeholder">No recorded frame yet. The line uses fractions of the frame, so it stays put when one appears.</span> : null}
        <svg className="line-overlay" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
          <line x1={ax} y1={ay} x2={bx} y2={by} className="line-stroke" />
          {direction !== "reverse" ? arrow(1) : null}
          {direction !== "forward" ? arrow(-1) : null}
        </svg>
        {(["a", "b"] as const).map(end => {
          const x = end === "a" ? line.x1 : line.x2, y = end === "a" ? line.y1 : line.y2;
          return (
            <button key={end} type="button" className="line-end" data-selected={selected === end}
                    aria-label={`Line end ${end.toUpperCase()}: ${percent(x)}% from left, ${percent(y)}% from top`}
                    style={{ left: `${x * 100}%`, top: `${y * 100}%` }}
                    onPointerDown={event => onEndDown(event, end)} onFocus={() => setSelected(end)} onKeyDown={event => onEndKey(event, end)}>
              <span aria-hidden="true">{end.toUpperCase()}</span>
            </button>
          );
        })}
      </div>
      <p className="muted field-hint">
        Objects count when their centre crosses the line {direction === "both" ? "in either direction" : direction === "forward" ? "in the arrow's direction" : "against the arrow"}.
        {!readOnly ? " Drag on the frame to draw a new line. Focus end A or B and use arrows to move it." : ""}
      </p>
      {short ? <p className="field-error" role="alert">Make the line at least 2% of the frame long.</p> : null}
      <table className="zone-table">
        <thead><tr><th scope="col">End</th><th scope="col">Left %</th><th scope="col">Top %</th></tr></thead>
        <tbody>
          {(["a", "b"] as const).map(end => (
            <tr key={end} data-selected={selected === end}>
              <th scope="row">{end.toUpperCase()}</th>
              {(["x", "y"] as const).map(axis => {
                const field = `${axis}${end === "a" ? 1 : 2}` as keyof Line;
                return (
                  <td key={axis}>
                    <input type="number" min={0} max={100} step={0.1} value={percent(line[field])} disabled={readOnly}
                           aria-label={`Line end ${end.toUpperCase()} ${axis === "x" ? "left" : "top"} percent`}
                           onChange={event => event.target.value !== "" && update({ ...line, [field]: Number(event.target.value) / 100 })} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
