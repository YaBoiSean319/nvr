import { useEffect, useMemo, useRef } from "react";
import { ApiError, latestOnly } from "@/api/client";
import type { DisplaySettings, TimelineCamera } from "@/api/types";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { playbackClock } from "./clock";
import { eventDestination, eventTimes, frameNeighbour, intervalAt, skipEmpty, toMicroIso, type Direction } from "./investigationRules";
import { usePlaybackStore } from "./store";

interface Props {
  settings: DisplaySettings;
  range: { start: number; end: number } | null;
  /** Timeline rows for the searched cameras, in display order. */
  rows: TimelineCamera[];
  names: Map<string, string>;
}

export function isEditable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

/** Step frames, jump between events, skip empty time and take snapshots of the active camera. */
export function Investigation({ settings, range, rows, names }: Props) {
  const activeId = usePlaybackStore(store => store.activeId);
  const eventScope = usePlaybackStore(store => store.eventScope);
  const skip = usePlaybackStore(store => store.skipEmpty);
  const message = usePlaybackStore(store => store.message);
  const { setEventScope, setSkipEmpty, setMessage } = usePlaybackStore.getState();
  const active = rows.find(row => row.camera_id === activeId) ?? rows[0];
  const ready = Boolean(range && rows.length);

  // Only the newest frame request may move the playhead; older answers are discarded.
  const stepFrame = useRef(latestOnly((signal, camera: TimelineCamera, at: number, direction: Direction, bounds: { start: number; end: number }) =>
    frameNeighbour(camera, at, direction, bounds, signal)));

  useEffect(() => {
    playbackClock.skip = skip && rows.length ? skipEmpty(rows) : null;
    return () => {
      playbackClock.skip = null;
    };
  }, [skip, rows]);

  const events = useMemo(() => {
    if (!range) return [];
    return eventTimes(eventScope === "active" && active ? [active] : rows, range);
  }, [rows, range, eventScope, active]);

  async function frame(direction: Direction) {
    if (!range || !active) return;
    playbackClock.pause();
    const name = names.get(active.camera_id) ?? "this camera";
    try {
      const found = await stepFrame.current(active, playbackClock.time(), direction, range);
      if (found === undefined) return; // superseded by a newer step
      if (found === null) {
        setMessage(`No ${direction} frame for ${name} within this search.`);
        return;
      }
      setMessage(null);
      playbackClock.seek(found);
    } catch (error) {
      setMessage(error instanceof ApiError ? error.detail : "The frame could not be found.");
    }
  }

  function event(direction: Direction) {
    if (!range) return;
    const destination = eventDestination(events, playbackClock.time(), direction);
    if (destination === null) {
      const where = eventScope === "active" && active ? ` for ${names.get(active.camera_id) ?? "the active camera"}` : "";
      setMessage(`No ${direction === "next" ? "later" : "earlier"} event${where} in this search.`);
      return;
    }
    setMessage(null);
    playbackClock.seek(destination); // play state is kept
  }

  async function snapshot() {
    if (!active) return;
    playbackClock.pause();
    const at = playbackClock.time();
    const name = names.get(active.camera_id) ?? "Camera";
    const interval = intervalAt(active, at);
    if (!interval) {
      setMessage(`${name} has no recording at ${formatSiteDateTime(at, settings)}. Move to recorded footage to take a snapshot.`);
      return;
    }
    const url = `/api/cameras/${encodeURIComponent(active.camera_id)}/segments/${interval.id}/snapshot?at=${encodeURIComponent(toMicroIso(at))}`;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { detail?: string } | null;
        setMessage(body?.detail ?? "The snapshot could not be taken.");
        return;
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      const sourceTime = response.headers.get("x-source-time") ?? new Date(at).toISOString();
      link.download = `${name.replace(/[^\w.-]+/g, "_")}_${sourceTime.replace(/[:+]/g, "-")}.jpg`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
      const reference = response.headers.get("x-audit-reference");
      setMessage(`Snapshot of ${name} at ${formatSiteDateTime(Date.parse(sourceTime), settings)} saved${reference ? ` (${reference})` : ""}.`);
    } catch {
      setMessage("The NVR could not be reached to take the snapshot.");
    }
  }

  // Keyboard shortcuts from the earlier interface; typing in a field never triggers them.
  const handlers = useRef({ frame, event });
  handlers.current = { frame, event };
  useEffect(() => {
    if (!ready) return;
    const onKey = (key: KeyboardEvent) => {
      if (key.defaultPrevented || key.altKey || key.ctrlKey || key.metaKey || isEditable(key.target)) return;
      if (document.querySelector("[role=dialog]")) return;
      if (key.code === "Space") {
        if (key.target instanceof HTMLButtonElement) return; // Space activates the focused button
        key.preventDefault();
        if (playbackClock.playing) playbackClock.pause();
        else playbackClock.play();
      } else if (key.key === "ArrowLeft" || key.key === "ArrowRight") {
        key.preventDefault();
        const direction = key.key === "ArrowLeft" ? "previous" : "next";
        if (key.shiftKey) handlers.current.event(direction);
        else playbackClock.seek(playbackClock.time() + (direction === "previous" ? -5_000 : 5_000));
      } else if (key.key === "," || key.key === ".") {
        key.preventDefault();
        void handlers.current.frame(key.key === "," ? "previous" : "next");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready]);

  return (
    <div className="investigation" role="group" aria-label="Investigation">
      <button type="button" className="button icon-button" aria-label="Previous frame" title="Previous frame (,)" disabled={!ready} onClick={() => void frame("previous")}>⏮</button>
      <button type="button" className="button icon-button" aria-label="Next frame" title="Next frame (.)" disabled={!ready} onClick={() => void frame("next")}>⏭</button>
      <button type="button" className="button" disabled={!ready} title="Previous event (Shift+←)" onClick={() => event("previous")}>◀ Event</button>
      <button type="button" className="button" disabled={!ready} title="Next event (Shift+→)" onClick={() => event("next")}>Event ▶</button>
      <select aria-label="Events from" value={eventScope} onChange={change => setEventScope(change.target.value as "all" | "active")}>
        <option value="all">All cameras</option>
        <option value="active">Active camera</option>
      </select>
      <label className="investigation-skip">
        <input type="checkbox" checked={skip} onChange={change => setSkipEmpty(change.target.checked)} />
        Skip empty periods
      </label>
      <button type="button" className="button" disabled={!ready} onClick={() => void snapshot()}>Snapshot</button>
      {message ? (
        <p className="investigation-message" role="status">
          {message}
          <button type="button" className="button" data-variant="ghost" aria-label="Dismiss message" onClick={() => setMessage(null)}>✕</button>
        </p>
      ) : null}
    </div>
  );
}
