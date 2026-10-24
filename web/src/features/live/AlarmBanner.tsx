import { useDisplaySettings } from "@/api/queries";
import { formatSiteTime } from "@/lib/zonedTime";
import { useLiveStore, type PopIn } from "./store";

const capitalised = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

export function popInSummary(alarm: PopIn, time: string): string {
  return `${alarm.rule_name}: ${alarm.camera_name} · ${capitalised(alarm.label)}${time ? ` at ${time}` : ""}`;
}

/** Shown over Live while an alarm layout replaces the person's own: what it is, what waits, and the way back. */
export function AlarmBanner() {
  const layout = useLiveStore(store => store.alarmLayout);
  const display = useDisplaySettings();
  const { nextAlarm, returnToMyView } = useLiveStore.getState();
  if (!layout) return null;
  const time = display.data ? formatSiteTime(Date.parse(layout.current.occurred_at), display.data, true) : "";
  return (
    <div className="alarm-banner" role="region" aria-label="Alarm layout">
      <span className="status-dot" data-tone="bad" aria-hidden="true" />
      <strong>Alarm</strong>
      <span className="alarm-banner-text">{popInSummary(layout.current, time)}</span>
      {layout.queue.length ? (
        <button type="button" className="button" onClick={nextAlarm}>Next alarm ({layout.queue.length} waiting)</button>
      ) : null}
      <button type="button" className="button" data-variant="primary" onClick={returnToMyView}>Return to my view</button>
    </div>
  );
}
