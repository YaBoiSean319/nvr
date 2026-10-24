import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Camera, TimelineCamera } from "@/api/types";
import { autoGrid, fitTiles } from "@/lib/layout";
import { PlaybackTile } from "./PlaybackTile";
import { usePlaybackStore } from "./store";

const GAP = 4;

/** Auto-tiled playback video; tiles keep their DOM position so adding a camera never reloads the others. */
export function PlaybackGrid({ cameras, timeline }: { cameras: Map<string, Camera>; timeline: Map<string, TimelineCamera> }) {
  const cameraIds = usePlaybackStore(store => store.cameraIds);
  const activeId = usePlaybackStore(store => store.activeId);
  const enlargedId = usePlaybackStore(store => store.enlargedId);
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
  });

  useEffect(() => {
    if (!enlargedId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") usePlaybackStore.getState().unenlarge();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enlargedId]);

  const shown = cameraIds.filter(id => cameras.has(id));
  if (!shown.length) {
    return (
      <div ref={container} className="grid-empty">
        <p><strong>Choose cameras to review footage.</strong></p>
        <p className="muted">Select cameras in the browser, then search a time range.</p>
      </div>
    );
  }
  const { columns, rows } = enlargedId ? { columns: 1, rows: 1 } : autoGrid(shown.length);
  const { tileWidth, tileHeight } = fitTiles(size.width, size.height, columns, rows, GAP);
  return (
    <div ref={container} className="grid-area">
      <div className="grid" data-columns={columns} data-rows={rows}
           style={{ gridTemplateColumns: `repeat(${columns}, ${tileWidth}px)`, gridTemplateRows: `repeat(${rows}, ${tileHeight}px)`, gap: GAP }}>
        {shown.map((id, index) => {
          const hidden = Boolean(enlargedId && enlargedId !== id);
          const style = enlargedId === id ? { gridColumn: 1, gridRow: 1 } : { gridColumn: (index % columns) + 1, gridRow: Math.floor(index / columns) + 1 };
          return (
            <div key={id} className="grid-cell" data-position={index} hidden={hidden} style={style}>
              <PlaybackTile id={id} name={cameras.get(id)!.name} camera={timeline.get(id)} active={activeId === id}
                            enlarged={enlargedId === id} hidden={hidden} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
