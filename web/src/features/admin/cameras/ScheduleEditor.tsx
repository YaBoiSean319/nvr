import type { ScheduleInterval, ScheduleMode } from "./api";

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const MINUTES_PER_DAY = 1440;
const MODE_LABEL: Record<ScheduleMode, string> = { continuous: "Continuous", motion: "Motion", disabled: "Disabled" };

const minutes = (time: string) => Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5));

/**
 * The server's rules, checked as you type: weekdays 0=Monday..6=Sunday, start-inclusive and
 * end-exclusive, an end before the start runs overnight and belongs to its start day, and
 * no two intervals may cover the same minute of the week.
 */
export function scheduleProblem(intervals: ScheduleInterval[]): string | null {
  const occupied = new Map<number, number>();
  for (const [index, interval] of intervals.entries()) {
    const name = `Interval ${index + 1}`;
    if (!interval.days.length) return `${name} needs at least one day.`;
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(interval.start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(interval.end)) return `${name} needs a start and end time.`;
    const start = minutes(interval.start), end = minutes(interval.end);
    if (start === end) return `${name} starts and ends at the same time.`;
    for (const day of interval.days) {
      const first = day * MINUTES_PER_DAY + start;
      const last = day * MINUTES_PER_DAY + end + (end < start ? MINUTES_PER_DAY : 0);
      for (let minute = first; minute < last; minute += 1) {
        const slot = minute % (7 * MINUTES_PER_DAY);
        const other = occupied.get(slot);
        if (other !== undefined && other !== index) {
          return `Interval ${index + 1} overlaps interval ${other + 1} on ${DAY_NAMES[Math.floor(slot / MINUTES_PER_DAY)]} at ${String(Math.floor((slot % MINUTES_PER_DAY) / 60)).padStart(2, "0")}:${String(slot % 60).padStart(2, "0")}.`;
        }
        occupied.set(slot, index);
      }
    }
  }
  return null;
}

/** Bands per weekday, splitting overnight intervals at midnight. */
function bands(intervals: ScheduleInterval[]) {
  const result: { day: number; start: number; end: number; mode: ScheduleMode; index: number; continued: boolean }[] = [];
  intervals.forEach((interval, index) => {
    const start = minutes(interval.start), end = minutes(interval.end);
    if (Number.isNaN(start) || Number.isNaN(end) || start === end) return;
    for (const day of interval.days) {
      if (end > start) result.push({ day, start, end, mode: interval.mode, index, continued: false });
      else {
        result.push({ day, start, end: MINUTES_PER_DAY, mode: interval.mode, index, continued: false });
        if (end > 0) result.push({ day: (day + 1) % 7, start: 0, end, mode: interval.mode, index, continued: true });
      }
    }
  });
  return result;
}

export function ScheduleEditor({ intervals, onChange, readOnly = false, withModes = true }: {
  intervals: ScheduleInterval[]; onChange: (intervals: ScheduleInterval[]) => void; readOnly?: boolean;
  /** Recording schedules choose a mode per interval; other schedules are simply active inside their intervals. */
  withModes?: boolean;
}) {
  const set = (index: number, change: Partial<ScheduleInterval>) => onChange(intervals.map((interval, position) => (position === index ? { ...interval, ...change } : interval)));
  const toggleDay = (index: number, day: number) => {
    const days = intervals[index]!.days;
    set(index, { days: (days.includes(day) ? days.filter(existing => existing !== day) : [...days, day]).sort() });
  };
  return (
    <div className="schedule-editor">
      <div className="schedule-week" role="img" aria-label={withModes ? "Weekly schedule overview; times outside every interval are not recorded" : "Weekly schedule overview; the schedule is active only inside its intervals"}>
        <div className="schedule-hours" aria-hidden="true">{[0, 6, 12, 18, 24].map(hour => <span key={hour} style={{ left: `${(hour / 24) * 100}%` }}>{String(hour).padStart(2, "0")}</span>)}</div>
        {DAYS.map((day, dayIndex) => (
          <div key={day} className="schedule-day">
            <span className="schedule-day-name">{day}</span>
            <div className="schedule-track">
              {bands(intervals).filter(band => band.day === dayIndex).map((band, position) => (
                <span key={position} className="schedule-band" data-mode={withModes ? band.mode : "continuous"} title={`Interval ${band.index + 1}${withModes ? `: ${MODE_LABEL[band.mode]}` : ""}${band.continued ? " (continued overnight)" : ""}`}
                      style={{ left: `${(band.start / MINUTES_PER_DAY) * 100}%`, width: `${((band.end - band.start) / MINUTES_PER_DAY) * 100}%` }}>
                  {band.index + 1}
                </span>
              ))}
            </div>
          </div>
        ))}
        <ul className="schedule-legend" aria-hidden="true">
          {withModes ? (Object.keys(MODE_LABEL) as ScheduleMode[]).map(mode => <li key={mode}><i data-mode={mode} />{MODE_LABEL[mode]}</li>) : <li><i data-mode="continuous" />Active</li>}
          <li><i data-mode="none" />{withModes ? "Not scheduled (not recorded)" : "Not active"}</li>
        </ul>
      </div>
      <ol className="schedule-list">
        {intervals.map((interval, index) => (
          <li key={index} className="schedule-row">
            <fieldset aria-label={`Interval ${index + 1}`} disabled={readOnly}>
              <legend>Interval {index + 1}{minutes(interval.end) < minutes(interval.start) ? " · overnight" : ""}</legend>
              <div className="schedule-days">
                {DAYS.map((day, dayIndex) => (
                  <label key={day} className="day-toggle">
                    <input type="checkbox" checked={interval.days.includes(dayIndex)} onChange={() => toggleDay(index, dayIndex)} aria-label={`${DAY_NAMES[dayIndex]} in interval ${index + 1}`} />
                    <span aria-hidden="true">{day}</span>
                  </label>
                ))}
              </div>
              <label>From <input type="time" value={interval.start} onChange={event => set(index, { start: event.target.value })} aria-label={`Interval ${index + 1} start`} /></label>
              <label>To <input type="time" value={interval.end} onChange={event => set(index, { end: event.target.value })} aria-label={`Interval ${index + 1} end`} /></label>
              {withModes ? (
                <label>Mode
                  <select value={interval.mode} onChange={event => set(index, { mode: event.target.value as ScheduleMode })} aria-label={`Interval ${index + 1} mode`}>
                    {(Object.keys(MODE_LABEL) as ScheduleMode[]).map(mode => <option key={mode} value={mode}>{MODE_LABEL[mode]}</option>)}
                  </select>
                </label>
              ) : null}
              {!readOnly ? <button type="button" className="button" data-variant="ghost" onClick={() => onChange(intervals.filter((_, position) => position !== index))}>Remove interval {index + 1}</button> : null}
            </fieldset>
          </li>
        ))}
      </ol>
      {!readOnly ? (
        <button type="button" className="button" disabled={intervals.length >= 200}
                onClick={() => onChange([...intervals, { days: [], start: "08:00", end: "17:00", mode: "continuous" }])}>Add interval</button>
      ) : null}
      <p className="muted field-hint">
        Times are wall-clock times in the schedule's time zone. An interval that ends before it starts runs overnight and belongs to the day it starts.
        When clocks fall back, a repeated hour follows the interval both times; when they spring forward, the interval starts at the first real minute.
      </p>
    </div>
  );
}
