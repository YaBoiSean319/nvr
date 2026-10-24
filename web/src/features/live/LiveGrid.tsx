import { useEffect, useLayoutEffect, useRef, useState, type DragEvent } from "react";
import type { Camera } from "@/api/types";
import { autoGrid, fitTiles } from "@/lib/layout";
import { useLiveStore } from "./store";
import { VideoTile } from "./VideoTile";

const GAP = 4;
import { CAMERA_DRAG_TYPE } from "@/features/cameras/CameraBrowser";

function draggedCamera(event: DragEvent): string | null {
  return event.dataTransfer.getData(CAMERA_DRAG_TYPE) || null;
}

function acceptsCamera(event: DragEvent): boolean {
  return event.dataTransfer.types.includes(CAMERA_DRAG_TYPE);
}

/**
 * Auto layout follows the selected-camera count; a manual layout keeps fixed
 * positions. Tiles stay in the DOM in the order they were first added and are
 * placed with CSS grid coordinates, so rearranging never moves or reconnects a
 * playing video.
 */
export function LiveGrid({ cameras, onReplaceRequest }: { cameras: Map<string, Camera>; onReplaceRequest: (id: string) => void }) {
  const arrangement = useLiveStore(store => store.arrangement);
  const activeId = useLiveStore(store => store.activeId);
  const enlargedId = useLiveStore(store => store.enlargedId);
  const { replace, place, toggle } = useLiveStore.getState();
  const container = useRef<HTMLDivElement>(null);
  const mountOrder = useRef<string[]>([]);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  });

  useEffect(() => {
    if (!enlargedId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") useLiveStore.getState().unenlarge();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enlargedId]);

  const positions = new Map<string, number>();
  const known = (id: string | null): id is string => id !== null && cameras.has(id);
  if (arrangement.mode === "auto") arrangement.order.filter(known).forEach((id, index) => positions.set(id, index));
  else arrangement.slots.forEach((id, index) => known(id) && positions.set(id, index));
  mountOrder.current = [...mountOrder.current.filter(id => positions.has(id)), ...[...positions.keys()].filter(id => !mountOrder.current.includes(id))];

  const { columns, rows } = enlargedId
    ? { columns: 1, rows: 1 }
    : arrangement.mode === "manual"
      ? { columns: Math.sqrt(arrangement.slots.length), rows: Math.sqrt(arrangement.slots.length) }
      : autoGrid(positions.size);
  const { tileWidth, tileHeight } = fitTiles(size.width, size.height, columns, rows, GAP);

  function dropOnArea(event: DragEvent) {
    const id = draggedCamera(event);
    if (!id || arrangement.mode !== "auto" || positions.has(id)) return;
    event.preventDefault();
    toggle(id);
  }

  if (!positions.size && arrangement.mode === "auto") {
    return (
      <div ref={container} className="grid-empty" onDragOver={event => acceptsCamera(event) && event.preventDefault()} onDrop={dropOnArea}>
        <p><strong>Choose cameras to start watching.</strong></p>
        <p className="muted">Select a camera in the browser, or drag one here, and the view arranges itself.</p>
      </div>
    );
  }

  const coordinates = (index: number) => ({ gridColumn: (index % columns) + 1, gridRow: Math.floor(index / columns) + 1 });
  return (
    <div ref={container} className="grid-area" onDragOver={event => acceptsCamera(event) && event.preventDefault()} onDrop={dropOnArea}>
      <div
        className="grid"
        data-columns={columns}
        data-rows={rows}
        data-mode={arrangement.mode}
        style={{ gridTemplateColumns: `repeat(${columns}, ${tileWidth}px)`, gridTemplateRows: `repeat(${rows}, ${tileHeight}px)`, gap: GAP }}
      >
        {mountOrder.current.map(id => {
          const hidden = Boolean(enlargedId && enlargedId !== id);
          return (
            <div
              key={id}
              className="grid-cell"
              data-position={positions.get(id)}
              hidden={hidden}
              style={enlargedId === id ? { gridColumn: 1, gridRow: 1 } : coordinates(positions.get(id)!)}
              onDragOver={event => {
                if (acceptsCamera(event)) {
                  event.preventDefault();
                  event.stopPropagation();
                }
              }}
              onDrop={event => {
                const incoming = draggedCamera(event);
                if (!incoming) return;
                event.preventDefault();
                event.stopPropagation();
                replace(id, incoming);
              }}
            >
              <VideoTile camera={cameras.get(id)!} active={activeId === id} enlarged={enlargedId === id} suspended={hidden}
                         onReplaceRequest={() => onReplaceRequest(id)} />
            </div>
          );
        })}
        {!enlargedId && arrangement.mode === "manual"
          ? arrangement.slots.map((id, index) =>
              known(id) ? null : (
                <div
                  key={`empty-${index}`}
                  className="grid-cell grid-slot"
                  style={coordinates(index)}
                  data-position={index}
                  onDragOver={event => {
                    if (acceptsCamera(event)) {
                      event.preventDefault();
                      event.stopPropagation();
                    }
                  }}
                  onDrop={event => {
                    const incoming = draggedCamera(event);
                    if (!incoming) return;
                    event.preventDefault();
                    event.stopPropagation();
                    place(index, incoming);
                  }}
                >
                  <span className="muted">Empty position</span>
                </div>
              ),
            )
          : null}
      </div>
    </div>
  );
}
