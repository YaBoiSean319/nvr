import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/api/client";
import { keys } from "@/api/queries";
import type { Camera } from "@/api/types";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { deleteRetention, getRetention, getSchedule, putRetention, putSchedule, type CameraRetention, type CameraSchedule, type ScheduleInterval } from "./api";
import { MODE_LABEL } from "./inventory";
import { ScheduleEditor, scheduleProblem } from "./ScheduleEditor";

interface Draft {
  schedule: { override: boolean; timezone: string; intervals: ScheduleInterval[] };
  retention: { override: boolean; days: number };
}

const ZONES: string[] = (() => {
  try {
    return (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  } catch {
    return [];
  }
})();

function fromServer(schedule: CameraSchedule, retention: CameraRetention): Draft {
  return {
    // Without an override only the choice matters; the inherited intervals are shown read-only.
    schedule: schedule.source === "camera" && schedule.configured
      ? { override: true, timezone: schedule.timezone, intervals: schedule.intervals }
      : { override: false, timezone: "", intervals: [] },
    retention: { override: !retention.inherits_group, days: retention.retention_days },
  };
}

const SOURCE_LABEL: Record<string, string> = { camera: "this camera", group: "its group", default: "the system default", system: "the system default" };

/** Weekly recording schedule and footage retention, each overriding or inheriting the group. */
export function ScheduleTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const scheduleKey = ["cameras", camera.id, "schedule"] as const;
  const retentionKey = ["cameras", camera.id, "retention"] as const;
  const schedule = useQuery({ queryKey: scheduleKey, queryFn: ({ signal }) => getSchedule(camera.id, signal) });
  const retention = useQuery({ queryKey: retentionKey, queryFn: ({ signal }) => getRetention(camera.id, signal) });
  const hasGroup = Boolean(camera.group_name);
  const current = useMemo(() => (schedule.data && retention.data ? fromServer(schedule.data, retention.data) : undefined), [schedule.data, retention.data]);
  const tab = useTabDraft(current, {
    label: `Schedule & Retention for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      const saved: string[] = [];
      const failed: string[] = [];
      let next = base;
      if (JSON.stringify(draft.schedule) !== JSON.stringify(base.schedule)) {
        try {
          if (draft.schedule.override) await putSchedule(camera.id, { timezone: draft.schedule.timezone.trim(), intervals: draft.schedule.intervals, inherit_group: false });
          else if (hasGroup) await putSchedule(camera.id, { timezone: "UTC", intervals: [], inherit_group: true });
          else await api<void>(`/api/cameras/${encodeURIComponent(camera.id)}/schedule`, { method: "DELETE" });
          const fresh = await getSchedule(camera.id);
          client.setQueryData(scheduleKey, fresh);
          next = { ...next, schedule: fromServer(fresh, retention.data!).schedule };
          saved.push("schedule");
        } catch (error) {
          failed.push(`Schedule not saved: ${failureText(error)}.`);
        }
      }
      if (JSON.stringify(draft.retention) !== JSON.stringify(base.retention)) {
        try {
          if (draft.retention.override) await putRetention(camera.id, draft.retention.days);
          else await deleteRetention(camera.id);
          const fresh = await getRetention(camera.id);
          client.setQueryData(retentionKey, fresh);
          next = { ...next, retention: fromServer(schedule.data!, fresh).retention };
          saved.push("retention");
        } catch (error) {
          failed.push(`Retention not saved: ${failureText(error)}.`);
        }
      }
      void client.invalidateQueries({ queryKey: keys.cameras });
      if (failed.length) return { ok: false, base: next, outcome: { tone: saved.length ? "warn" : "bad", text: `${saved.length ? `Saved ${saved.join(" and ")}. ` : ""}${failed.join(" ")}` } };
      return { ok: true, base: next, outcome: { tone: "good", text: `Saved ${saved.join(" and ")}.` } };
    },
  });

  if (schedule.isPending || retention.isPending) return <p className="muted">Loading schedule and retention…</p>;
  if (schedule.error || retention.error || !tab.draft) {
    return <p className="notice" data-tone="bad" role="alert">{failureText(schedule.error ?? retention.error, "Schedule and retention could not be loaded.")}</p>;
  }
  const draft = tab.draft;
  const data = schedule.data!;
  const kept = retention.data!;
  const readOnly = !admin;
  const problem = draft.schedule.override
    ? !draft.schedule.timezone.trim() ? "Choose the schedule's time zone." : scheduleProblem(draft.schedule.intervals)
    : null;
  const retentionProblem = draft.retention.override && !(draft.retention.days >= 1 && draft.retention.days <= 3650) ? "Keep footage for 1 to 3650 days." : null;

  function setSchedule(change: Partial<Draft["schedule"]>) {
    tab.change(current => ({ ...current, schedule: { ...current.schedule, ...change } }));
  }

  return (
    <form className="tab-form tab-form-wide" aria-label="Schedule and retention settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice" role="status">
        Recording now: <strong>{MODE_LABEL[data.effective_recording_mode]}</strong>. Schedule from {data.configured ? SOURCE_LABEL[data.source] : "no schedule (the camera's own mode applies)"}.
        {" "}Retention: {kept.retention_days} days from {SOURCE_LABEL[kept.policy_source] ?? kept.policy_source}.
      </p>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Weekly schedule</legend>
        <div className="segmented" role="radiogroup" aria-label="Schedule source">
          <label><input type="radio" name="schedule-source" checked={!draft.schedule.override} onChange={() => setSchedule({ override: false, timezone: "", intervals: [] })} />
            {hasGroup ? `Use the ${camera.group_name} group's schedule` : "No schedule (use the camera's recording mode)"}</label>
          <label><input type="radio" name="schedule-source" checked={draft.schedule.override}
                        onChange={() => setSchedule({ override: true, intervals: data.intervals, timezone: data.timezone })} />
            This camera's own schedule</label>
        </div>
        {draft.schedule.override ? (
          <>
            <label className="field"><span>Time zone</span>
              <input list="schedule-zones" value={draft.schedule.timezone} onChange={event => setSchedule({ timezone: event.target.value })} autoComplete="off" />
              <datalist id="schedule-zones">{ZONES.map(zone => <option key={zone} value={zone} />)}</datalist>
            </label>
            <ScheduleEditor intervals={draft.schedule.intervals} onChange={intervals => setSchedule({ intervals })} readOnly={readOnly} />
          </>
        ) : data.configured ? (
          <>
            <p className="muted field-hint">Inherited from {SOURCE_LABEL[data.source]} in {data.timezone}:</p>
            <ScheduleEditor intervals={data.intervals} onChange={() => undefined} readOnly />
          </>
        ) : null}
      </fieldset>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Retention</legend>
        <label className="check"><input type="checkbox" checked={draft.retention.override}
               onChange={event => tab.change(current => ({ ...current, retention: { ...current.retention, override: event.target.checked } }))} /> Keep this camera's footage for a specific time</label>
        <label className="field"><span>Days to keep footage</span>
          <input type="number" min={1} max={3650} value={draft.retention.days} disabled={!draft.retention.override}
                 onChange={event => tab.change(current => ({ ...current, retention: { ...current.retention, days: Number(event.target.value) } }))} />
        </label>
        <p className="muted field-hint">Footage under a hold, being played or being exported is never removed early.</p>
      </fieldset>
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={problem ?? retentionProblem} /> : null}
    </form>
  );
}
