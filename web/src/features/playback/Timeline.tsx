import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { Camera, DisplaySettings, TimelineCamera } from "@/api/types";
import { formatSiteDate, formatSiteDateTime, formatSiteTime, sameSiteDay, siteOffsetMs } from "@/lib/zonedTime";
import { playbackClock } from "./clock";
import { useClockTime, usePlaybackStore, type TimeWindow } from "./store";
import { isWholeRange, pan, percent, tickStep, ticks, visibleSpan, zoom } from "./timescale";
import { REASON_LABEL } from "./gapReasons";

const GAP_LABEL: Record<string, string> = {
  failure: "Recording failure",
  uncertain: "Coverage uncertain",
  expected_idle: "Not recording (expected)",
  missing: "No recording",
};

interface Props {
  settings: DisplaySettings;
  range: TimeWindow;
  cameraIds: string[];
  cameras: Map<string, Camera>;
  timeline: Map<string, TimelineCamera>;
  loading: boolean;
}

/**
 * Aligned camera rows under one time axis. The pane is bounded: rows scroll inside it,
 * never the page. Click or drag to seek; zoom and pan change only what is visible.
 */
export function Timeline({ settings, range, cameraIds, cameras, timeline, loading }: Props) {
  const stored = usePlaybackStore(store => store.window);
  const exportMarks = usePlaybackStore(store => store.marks);
  const { setWindow } = usePlaybackStore.getState();
  const window = stored ?? range;
  const time = useClockTime();
  const tracks = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const dragging = useRef(false);

  useLayoutEffect(() => {
    const element = tracks.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Ctrl/⌘ + wheel zooms around the pointer; horizontal or Shift + wheel pans. Plain wheel scrolls rows.
  useEffect(() => {
    const element = tracks.current?.closest<HTMLElement>(".timeline-body");
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      const current = usePlaybackStore.getState().window ?? range;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        apply(zoom(current, range, event.deltaY > 0 ? 1.25 : 0.8, timeAt(event.clientX, current)));
      } else if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.preventDefault();
        const delta = (event.shiftKey ? event.deltaY : event.deltaX) / Math.max(1, width);
        apply(pan(current, range, delta * (current.end - current.start)));
      }
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  });

  function apply(next: TimeWindow) {
    setWindow(isWholeRange(next, range) ? null : next);
  }

  function timeAt(clientX: number, current: TimeWindow = window): number {
    const rect = tracks.current!.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    return current.start + ratio * (current.end - current.start);
  }

  function onPointerDown(event: PointerEvent<HTMLElement>) {
    if (event.button !== 0) return;
    dragging.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    playbackClock.seek(timeAt(event.clientX));
  }

  function onPointerMove(event: PointerEvent<HTMLElement>) {
    if (dragging.current) playbackClock.seek(timeAt(event.clientX));
  }

  function onRulerKey(event: KeyboardEvent<HTMLDivElement>) {
    const span = window.end - window.start;
    const steps: Record<string, number> = {
      ArrowLeft: event.shiftKey ? -60_000 : -5_000, ArrowRight: event.shiftKey ? 60_000 : 5_000,
      PageDown: -span / 10, PageUp: span / 10,
    };
    if (event.key === "Home") playbackClock.seek(range.start);
    else if (event.key === "End") playbackClock.seek(range.end);
    else if (event.key in steps) playbackClock.seek(playbackClock.time() + steps[event.key]!);
    else return;
    event.preventDefault();
  }

  const step = tickStep(window, width);
  const marks = ticks(window, step, siteOffsetMs(window.start, settings.time_zone));
  const multiDay = !sameSiteDay(window.start, window.end, settings.time_zone);
  const cursor = percent(time, window);
  const span = window.end - window.start;
  const marked = exportMarks.in !== null || exportMarks.out !== null
    ? visibleSpan(Math.min(exportMarks.in ?? range.start, exportMarks.out ?? range.end), Math.max(exportMarks.in ?? range.start, exportMarks.out ?? range.end), window)
    : null;
  const rangeStyle = marked ? { left: `${marked.left}%`, width: `${marked.width}%` } : undefined;

  return (
    <section id="playback-timeline" className="timeline" aria-label="Timeline" data-window-start={Math.round(window.start)} data-window-end={Math.round(window.end)}
             data-mark-in={exportMarks.in === null ? undefined : Math.round(exportMarks.in)} data-mark-out={exportMarks.out === null ? undefined : Math.round(exportMarks.out)}>
      <div className="timeline-tools">
        <button type="button" className="button" onClick={() => apply(zoom(window, range, 0.5, Math.max(window.start, Math.min(window.end, time))))}
                disabled={span <= 10_000}>Zoom in</button>
        <button type="button" className="button" onClick={() => apply(zoom(window, range, 2, time))} disabled={!stored}>Zoom out</button>
        <button type="button" className="button" onClick={() => setWindow(null)} disabled={!stored}>Fit range</button>
        <button type="button" className="button icon-button" aria-label="Pan earlier" onClick={() => apply(pan(window, range, -span / 2))}
                disabled={window.start <= range.start}>◀</button>
        <button type="button" className="button icon-button" aria-label="Pan later" onClick={() => apply(pan(window, range, span / 2))}
                disabled={window.end >= range.end}>▶</button>
        <span className="muted timeline-span">
          {formatSiteDateTime(window.start, settings, false)} – {multiDay ? formatSiteDateTime(window.end, settings, false) : formatSiteTime(window.end, settings)}
        </span>
        <ul className="timeline-legend" aria-label="Timeline legend">
          <li><i data-mark="recording" />Recording</li>
          <li><i data-mark="backfilled" />Backfilled from the camera</li>
          <li><i data-mark="missing" />No recording</li>
          <li><i data-mark="failure" />Failure</li>
          <li><i data-mark="uncertain" />Uncertain</li>
          <li><i data-mark="motion" />Motion</li>
          <li><i data-mark="ai" />AI detection</li>
          <li><i data-mark="event" />Outside event</li>
          <li><i data-mark="unanalyzed" />Not analyzed</li>
        </ul>
        {loading ? <span className="spinner" aria-label="Loading timeline" /> : null}
      </div>
      <div className="timeline-body">
        <div className="timeline-row timeline-ruler-row">
          <span className="timeline-label muted">{multiDay ? "" : formatSiteDate(window.start, settings)}</span>
          <div ref={tracks} className="timeline-ruler" role="slider" tabIndex={0} aria-label="Playback position"
               aria-valuemin={range.start} aria-valuemax={range.end} aria-valuenow={Math.round(time)}
               aria-valuetext={formatSiteDateTime(time, settings)}
               onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => (dragging.current = false)} onKeyDown={onRulerKey}>
            {marks.map(mark => (
              <span key={mark} className="timeline-tick" style={{ left: `${percent(mark, window)}%` }}>
                {formatSiteTime(mark, settings, step < 60_000)}
              </span>
            ))}
            {marked ? <span className="timeline-range" data-testid="export-range" style={rangeStyle} /> : null}
            <span className="timeline-cursor-head" style={{ left: `${cursor}%` }} hidden={cursor < 0 || cursor > 100} />
          </div>
        </div>
        {cameraIds.filter(id => cameras.has(id)).map(id => {
          const row = timeline.get(id);
          const empty = row !== undefined && !row.segments.length;
          return (
            <div key={id} className="timeline-row" data-camera-id={id} data-footage={row ? (empty ? "none" : "some") : "loading"}>
              <span className="timeline-label" title={cameras.get(id)!.name}>
                <span className="timeline-name">{cameras.get(id)!.name}</span>
                {empty ? <span className="timeline-badge">No footage</span> : null}
              </span>
              <div className="timeline-track" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => (dragging.current = false)}>
                {row ? <Track row={row} window={window} settings={settings} /> : null}
                {marked ? <span className="timeline-range" style={rangeStyle} /> : null}
                <span className="timeline-cursor" style={{ left: `${cursor}%` }} hidden={cursor < 0 || cursor > 100} />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Track({ row, window, settings }: { row: TimelineCamera; window: TimeWindow; settings: DisplaySettings }) {
  const at = (iso: string) => Date.parse(iso);
  const describe = (start: number, end: number) => `${formatSiteTime(start, settings, true)} – ${formatSiteTime(end, settings, true)}`;
  return (
    <>
      {row.segments.map(segment => {
        const span = visibleSpan(at(segment.starts_at), at(segment.ends_at), window);
        const backfilled = segment.source === "backfill";
        return span ? <span key={`s${segment.id}`} className="timeline-mark" data-mark={backfilled ? "backfilled" : "recording"}
                            title={`${backfilled ? "Backfilled from the camera's own recording" : "Recording"} ${describe(at(segment.starts_at), at(segment.ends_at))}`}
                            style={{ left: `${span.left}%`, width: `${span.width}%` }} /> : null;
      })}
      {row.gaps.map(gap => {
        const span = visibleSpan(at(gap.starts_at), at(gap.ends_at), window);
        return span ? <span key={`g${gap.starts_at}`} className="timeline-mark" data-mark={gap.state} data-reason={gap.reason_code}
                            title={`${GAP_LABEL[gap.state] ?? gap.state}: ${REASON_LABEL[gap.reason_code] ?? gap.reason_code}. ${gap.reason}${gap.backfill ? ` Backfill ${gap.backfill.state}${gap.backfill.message ? `: ${gap.backfill.message}` : ""}.` : ""} (${describe(at(gap.starts_at), at(gap.ends_at))})`}
                            style={{ left: `${span.left}%`, width: `${span.width}%` }} /> : null;
      })}
      {row.ai_unanalyzed.map(interval => {
        const span = visibleSpan(at(interval.starts_at), interval.ends_at ? at(interval.ends_at) : window.end, window);
        return span ? <span key={`u${interval.starts_at}`} className="timeline-mark" data-mark="unanalyzed"
                            title={`AI not analyzed (${interval.state})${interval.detail ? `: ${interval.detail}` : ""}`}
                            style={{ left: `${span.left}%`, width: `${span.width}%` }} /> : null;
      })}
      {row.motion_events.map(event => {
        const position = percent(at(event.source_event_at), window);
        return position >= 0 && position <= 100
          ? <span key={`m${event.id}`} className="timeline-point" data-mark="motion" title={`Motion at ${formatSiteTime(at(event.source_event_at), settings, true)}`} style={{ left: `${position}%` }} />
          : null;
      })}
      {row.ai_detections.map(detection => {
        const position = percent(at(detection.first_seen_at), window);
        return position >= 0 && position <= 100
          ? <span key={`a${detection.id}`} className="timeline-point" data-mark="ai" data-label={detection.label}
                  title={`${detection.label === "person" ? "Person" : "Vehicle"} (${Math.round(detection.confidence * 100)}%) at ${formatSiteTime(at(detection.first_seen_at), settings, true)}`}
                  style={{ left: `${position}%` }} />
          : null;
      })}
      {(row.external_events ?? []).map(event => {
        const position = percent(at(event.occurred_at), window);
        return position >= 0 && position <= 100
          ? <span key={`x${event.id}`} className="timeline-point" data-mark="event" data-type={event.type}
                  title={`${event.label}${event.subject ? ` · ${event.subject}` : ""} (${event.source_name}) at ${formatSiteTime(at(event.occurred_at), settings, true)}${event.message ? `: ${event.message}` : ""}`}
                  style={{ left: `${position}%` }} />
          : null;
      })}
    </>
  );
}
