/**
 * Wall-clock times in the site time zone, independent of the browser's zone.
 *
 * Search inputs are `YYYY-MM-DDTHH:mm` strings meaning a local time at the site.
 * A fall-back hour repeats (two instants) and a spring-forward hour never happens
 * (no instant); callers must handle both explicitly.
 */
import type { DisplaySettings } from "@/api/types";

const formatters = new Map<string, Intl.DateTimeFormat>();

function fieldsFormatter(zone: string): Intl.DateTimeFormat {
  let formatter = formatters.get(zone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    });
    formatters.set(zone, formatter);
  }
  return formatter;
}

interface Fields { year: number; month: number; day: number; hour: number; minute: number; second: number }

function fieldsAt(instant: number, zone: string): Fields {
  const parts = Object.fromEntries(fieldsFormatter(zone).formatToParts(new Date(instant)).map(part => [part.type, part.value]));
  return {
    year: Number(parts.year), month: Number(parts.month), day: Number(parts.day),
    hour: Number(parts.hour) % 24, minute: Number(parts.minute), second: Number(parts.second),
  };
}

const LOCAL_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/** UTC instants (ms) that show this wall time in `zone`, earliest first: zero, one or two. */
export function localTimeCandidates(value: string, zone: string): number[] {
  const match = LOCAL_INPUT.exec(value);
  if (!match) return [];
  const [year, month, day, hour, minute, second = 0] = match.slice(1).map(value => Number(value ?? 0)) as [number, number, number, number, number, number];
  const target = Date.UTC(year, month - 1, day, hour, minute, second);
  const offsets = new Set<number>();
  for (let hours = -36; hours <= 36; hours += 6) {
    const probe = target + hours * 3_600_000;
    const fields = fieldsAt(probe, zone);
    offsets.add(Date.UTC(fields.year, fields.month - 1, fields.day, fields.hour, fields.minute, fields.second) - probe);
  }
  const matches = [...offsets].map(offset => target - offset).filter(instant => {
    const fields = fieldsAt(instant, zone);
    return fields.year === year && fields.month === month && fields.day === day && fields.hour === hour && fields.minute === minute && fields.second === second;
  });
  return [...new Set(matches)].sort((a, b) => a - b);
}

/** The `datetime-local` value for an instant at the site, optionally to the second. */
export function toLocalInput(instant: number, zone: string, seconds = false): string {
  const f = fieldsAt(instant, zone);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${f.year}-${pad(f.month)}-${pad(f.day)}T${pad(f.hour)}:${pad(f.minute)}${seconds ? `:${pad(f.second)}` : ""}`;
}

/** Short UTC offset label for an instant at the site, such as "GMT-4". */
export function offsetLabel(instant: number, zone: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" })
    .formatToParts(new Date(instant)).find(part => part.type === "timeZoneName")?.value ?? zone;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const LONG_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function clock(f: Fields, settings: DisplaySettings, seconds: boolean): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const tail = seconds ? `:${pad(f.second)}` : "";
  if (settings.clock_format === "24") return `${pad(f.hour)}:${pad(f.minute)}${tail}`;
  const hour = f.hour % 12 || 12;
  return `${hour}:${pad(f.minute)}${tail} ${f.hour < 12 ? "AM" : "PM"}`;
}

function date(f: Fields, settings: DisplaySettings): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  switch (settings.date_format) {
    case "long": return `${LONG_MONTHS[f.month - 1]} ${f.day}, ${f.year}`;
    case "iso": return `${f.year}-${pad(f.month)}-${pad(f.day)}`;
    case "day-month": return `${pad(f.day)}/${pad(f.month)}/${f.year}`;
    case "month-day": return `${pad(f.month)}/${pad(f.day)}/${f.year}`;
    default: return `${MONTHS[f.month - 1]} ${f.day}, ${f.year}`;
  }
}

/** Date and time at the site, in the site's chosen formats (matches the server's rendering). */
export function formatSiteDateTime(instant: number, settings: DisplaySettings, seconds = true): string {
  const f = fieldsAt(instant, settings.time_zone);
  return `${date(f, settings)} ${clock(f, settings, seconds)}`;
}

export function formatSiteTime(instant: number, settings: DisplaySettings, seconds = false): string {
  return clock(fieldsAt(instant, settings.time_zone), settings, seconds);
}

/** Whether two instants fall on the same site calendar day. */
export function sameSiteDay(a: number, b: number, zone: string): boolean {
  const x = fieldsAt(a, zone), y = fieldsAt(b, zone);
  return x.year === y.year && x.month === y.month && x.day === y.day;
}

export function formatSiteDate(instant: number, settings: DisplaySettings): string {
  return date(fieldsAt(instant, settings.time_zone), settings);
}

/** How far the site's wall clock is ahead of UTC at an instant (ms). */
export function siteOffsetMs(instant: number, zone: string): number {
  const f = fieldsAt(instant, zone);
  return Date.UTC(f.year, f.month - 1, f.day, f.hour, f.minute, f.second) - Math.floor(instant / 1000) * 1000;
}
