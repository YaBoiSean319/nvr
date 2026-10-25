import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import type { PopIn } from "./store";

const POLL_MS = 2_000;

/**
 * Follow alarms that pop into Live, from the moment this starts: alarms raised
 * earlier are never replayed. Each new alarm is handed over once, oldest first.
 */
export function useAlarmPopIns(enabled: boolean, onAlarm: (alarm: PopIn) => void) {
  const handler = useRef(onAlarm);
  handler.current = onAlarm;

  useEffect(() => {
    if (!enabled) return;
    let cursor: string | null = null;
    let stopped = false;
    let timer = 0;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const query = cursor === null ? "" : `?after=${encodeURIComponent(cursor)}`;
        const body = await api<{ cursor: string; alarms: PopIn[] }>(`/api/live/alarm-pop-ins${query}`, { signal: controller.signal });
        if (stopped) return;
        cursor = body.cursor;
        body.alarms.forEach(alarm => handler.current(alarm));
      } catch {
        // Try again at the next poll; a missed poll never loses an alarm, the cursor holds.
      }
      if (!stopped) timer = window.setTimeout(poll, POLL_MS);
    };
    void poll();
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [enabled]);
}
