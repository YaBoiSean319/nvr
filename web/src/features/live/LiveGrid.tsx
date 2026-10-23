import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Camera } from "@/api/types";
import { autoGrid, fitTiles } from "@/lib/layout";
import { useLiveStore } from "./store";
import { VideoTile } from "./VideoTile";

const GAP = 4;

/**
 * Auto layout follows the selected-camera count. Tiles are positioned with CSS
 * grid coordinates and keyed by camera, so adding, removing or reordering cameras
 * never remounts (and never reconnects) the others.
 */
export function LiveGrid({ cameras }: { cameras: Map<string, Camera> }) {
  const cameraIds = useLiveStore(store => store.cameraIds);
  const activeId = useLiveStore(store => store.activeId);
  const enlargedId = useLiveStore(store => store.enlargedId);
  const container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!enlargedId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") useLiveStore.getState().restore();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enlargedId]);

  const shown = cameraIds.filter(id => cameras.has(id));
  const { columns, rows } = enlargedId ? { columns: 1, rows: 1 } : autoGrid(shown.length);
  const { tileWidth, tileHeight } = fitTiles(size.width, size.height, columns, rows, GAP);

  if (!shown.length) {
    return (
      <div ref={container} className="grid-empty">
        <p><strong>Choose cameras to start watching.</strong></p>
        <p className="muted">Select a camera in the browser and the view arranges itself.</p>
      </div>
    );
  }

  return (
    <div ref={container} className="grid-area">
      <div
        className="grid"
        data-columns={columns}
        data-rows={rows}
        style={{
          gridTemplateColumns: `repeat(${columns}, ${tileWidth}px)`,
          gridTemplateRows: `repeat(${rows}, ${tileHeight}px)`,
          gap: GAP,
        }}
      >
        {shown.map((id, index) => {
          const hidden = Boolean(enlargedId && enlargedId !== id);
          return (
            <div
              key={id}
              className="grid-cell"
              hidden={hidden}
              style={enlargedId === id ? { gridColumn: 1, gridRow: 1 } : { gridColumn: (index % columns) + 1, gridRow: Math.floor(index / columns) + 1 }}
            >
              <VideoTile camera={cameras.get(id)!} active={activeId === id} enlarged={enlargedId === id} suspended={hidden} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
