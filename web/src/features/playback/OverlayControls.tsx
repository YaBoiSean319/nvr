import { useEffect } from "react";
import { playbackClock } from "./clock";
import { isEditable } from "./Investigation";
import { neighbour, type Direction } from "./overlay";
import { useTrackPath } from "./useOverlayData";
import { usePlaybackStore } from "./store";

/** Turn the detection boxes on and off (B), and step the followed object's stored appearances ([ and ]). */
export function OverlayControls({ ready }: { ready: boolean }) {
  const on = usePlaybackStore(store => store.overlayOn);
  const tracked = usePlaybackStore(store => store.tracked);
  const { points: path, loading } = useTrackPath(tracked?.id ?? null, tracked?.cameraId ?? null);
  const { setOverlayOn, trackObject, setMessage } = usePlaybackStore.getState();

  function step(direction: Direction) {
    if (!tracked) return;
    if (loading) {
      setMessage("The followed object's path is still loading.");
      return;
    }
    const at = neighbour(path, playbackClock.time(), direction);
    if (at === null) {
      setMessage(`No ${direction === "next" ? "later" : "earlier"} appearance of this object.`);
      return;
    }
    const { start, end } = playbackClock.range;
    if (at < start || at > end) {
      setMessage(`That appearance is outside this search (${direction === "next" ? "later" : "earlier"}).`);
      return;
    }
    setMessage(null);
    playbackClock.seek(at);
  }

  useEffect(() => {
    if (!ready) return;
    const onKey = (key: KeyboardEvent) => {
      if (key.defaultPrevented || key.altKey || key.ctrlKey || key.metaKey || isEditable(key.target) || document.querySelector("[role=dialog]")) return;
      if (key.key.toLowerCase() === "b") {
        key.preventDefault();
        setOverlayOn(!usePlaybackStore.getState().overlayOn);
      } else if ((key.key === "[" || key.key === "]") && usePlaybackStore.getState().tracked) {
        key.preventDefault();
        step(key.key === "[" ? "previous" : "next");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <span className="overlay-controls" role="group" aria-label="Object tracks">
      <button type="button" className="button" disabled={!ready} aria-pressed={on} title="Show detection boxes (B)" onClick={() => setOverlayOn(!on)}>Boxes</button>
      {on && tracked ? (
        <>
          <button type="button" className="button" title="Previous appearance of the followed object ([)" onClick={() => step("previous")}>◀ Object</button>
          <button type="button" className="button" title="Next appearance of the followed object (])" onClick={() => step("next")}>Object ▶</button>
          <button type="button" className="button" data-variant="ghost" onClick={() => trackObject(null)}>Stop following</button>
        </>
      ) : null}
    </span>
  );
}
