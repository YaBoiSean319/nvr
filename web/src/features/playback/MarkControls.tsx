import { useEffect } from "react";
import { playbackClock } from "./clock";
import { isEditable } from "./Investigation";
import { usePlaybackStore } from "./store";

/** Mark In/Out at the playhead, clear the marks, and open the export drawer. */
export function MarkControls({ ready }: { ready: boolean }) {
  const marks = usePlaybackStore(store => store.marks);
  const exportOpen = usePlaybackStore(store => store.exportOpen);
  const { setMark, clearMarks, setExportOpen } = usePlaybackStore.getState();

  useEffect(() => {
    if (!ready) return;
    const onKey = (key: KeyboardEvent) => {
      if (key.defaultPrevented || key.altKey || key.ctrlKey || key.metaKey || isEditable(key.target) || document.querySelector("[role=dialog]")) return;
      const letter = key.key.toLowerCase();
      if (letter !== "i" && letter !== "o") return;
      key.preventDefault();
      setMark(letter === "i" ? "in" : "out", playbackClock.time());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready, setMark]);

  const hasMarks = marks.in !== null || marks.out !== null;
  return (
    <span className="mark-controls" role="group" aria-label="Export marks">
      <button type="button" className="button" disabled={!ready} title="Mark in (I)" aria-pressed={marks.in !== null}
              onClick={() => setMark("in", playbackClock.time())}>Mark in</button>
      <button type="button" className="button" disabled={!ready} title="Mark out (O)" aria-pressed={marks.out !== null}
              onClick={() => setMark("out", playbackClock.time())}>Mark out</button>
      <button type="button" className="button" data-variant="ghost" disabled={!hasMarks} onClick={clearMarks}>Clear marks</button>
      <button type="button" className="button" disabled={!ready} aria-expanded={exportOpen} onClick={() => setExportOpen(!exportOpen)}>Export…</button>
    </span>
  );
}
