import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { keys, useCameraGroups } from "@/api/queries";
import type { Camera } from "@/api/types";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { assignGroup, getRetentionForecast, probeAudio, updateRecording, type ScheduleMode } from "./api";
import { MODE_LABEL } from "./inventory";

interface Draft {
  recording_enabled: boolean;
  recording_mode: ScheduleMode;
  pre_roll: number;
  post_roll: number;
  audio_enabled: boolean;
  group: string; // "" = no group
}

const published = (camera: Camera): Draft => ({
  recording_enabled: camera.recording_enabled,
  recording_mode: camera.recording_mode,
  pre_roll: camera.motion_pre_roll_seconds,
  post_roll: camera.motion_post_roll_seconds,
  audio_enabled: camera.audio_enabled,
  group: camera.group_name ?? "",
});

const AUDIO_LABEL: Record<string, string> = {
  enabled: "Recording audio",
  available_disabled: "Audio available, not recorded",
  unsupported: "No supported audio track",
  unknown: "Audio not checked yet",
};

/** Recording enablement, mode, buffers, audio and the policy group, saved explicitly. */
export function RecordingTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const groups = useCameraGroups();
  const [probe, setProbe] = useState<{ busy: boolean; text?: string; tone?: "good" | "bad" }>({ busy: false });
  const current = useMemo(() => published(camera), [camera]);
  const tab = useTabDraft(current, {
    label: `Recording for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      const saved: string[] = [];
      const failed: string[] = [];
      let next = { ...base };
      const recordingFields = ["recording_enabled", "recording_mode", "pre_roll", "post_roll", "audio_enabled"] as const;
      if (recordingFields.some(field => draft[field] !== base[field])) {
        try {
          const updated = await updateRecording(camera.id, {
            recording_enabled: draft.recording_enabled, recording_mode: draft.recording_mode,
            motion_pre_roll_seconds: draft.pre_roll, motion_post_roll_seconds: draft.post_roll,
            ...(draft.audio_enabled !== base.audio_enabled ? { audio_enabled: draft.audio_enabled } : {}),
          });
          next = { ...published(updated), group: next.group };
          saved.push("recording settings");
        } catch (error) {
          failed.push(`Recording settings not saved: ${failureText(error)}.`);
        }
      }
      if (draft.group !== base.group) {
        try {
          const result = await assignGroup(camera.id, draft.group || null);
          const outcome = result.results[0];
          if (outcome?.status === "success") {
            next = { ...next, group: draft.group };
            saved.push("group");
          } else {
            failed.push(`Group not saved: ${outcome?.detail ?? "the outcome is unknown; refresh before trying again"}.`);
          }
        } catch (error) {
          failed.push(`Group not saved: ${failureText(error)}.`);
        }
      }
      void client.invalidateQueries({ queryKey: keys.cameras });
      if (failed.length) return { ok: false, base: next, outcome: { tone: saved.length ? "warn" : "bad", text: `${saved.length ? `Saved ${saved.join(" and ")}. ` : ""}${failed.join(" ")}` } };
      return { ok: true, base: next, outcome: { tone: "good", text: `Saved ${saved.join(" and ")}.` } };
    },
  });
  const draft = tab.draft!;

  async function runProbe() {
    setProbe({ busy: true });
    try {
      const result = await probeAudio(camera.id);
      setProbe({ busy: false, tone: "good", text: result.audio_supported ? "The stream has a supported audio track." : "The stream has no supported audio track." });
      void client.invalidateQueries({ queryKey: keys.cameras });
    } catch (error) {
      setProbe({ busy: false, tone: "bad", text: failureText(error, "The audio check could not run.") });
    }
  }

  const policy = camera.recording_policy_source === "group" ? "group schedule" : camera.recording_policy_source === "camera" ? "camera schedule" : "camera setting";
  const effective = camera.effective_recording_mode ?? camera.recording_mode;
  const audioStatus = camera.audio_status ?? "unknown";
  const readOnly = !admin;
  const forecast = useQuery({ queryKey: ["cameras", camera.id, "retention-forecast"], queryFn: ({ signal }) => getRetentionForecast(camera.id, signal), refetchInterval: 5 * 60_000 });
  const shown = forecast.data?.camera;
  return (
    <form className="tab-form" aria-label="Recording settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice">
        Effective now: <strong>{MODE_LABEL[effective]}</strong> from the {policy}. Storage volume: {camera.storage_volume_id ?? "default"} (set by the policy group).
      </p>
      {shown ? (
        <section className="field-group" aria-label="Retention forecast">
          <h3 className="field-heading">Retention forecast</h3>
          <p className="notice" role="note" data-tone={shown.below_policy ? "warn" : shown.state === "ok" ? "good" : undefined}>
            {shown.message}
            {shown.below_policy && shown.days_until_shortfall !== null ? (shown.days_until_shortfall < 1 ? " · the disk is already full or will be within a day" : ` · the disk fills in about ${Math.round(shown.days_until_shortfall)} days`) : ""}
          </p>
          {shown.suggestions.length ? <ul className="plain-list" aria-label="Ways to keep the policy">{shown.suggestions.map(suggestion => <li key={suggestion}>{suggestion}</li>)}</ul> : null}
        </section>
      ) : null}
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Recording</legend>
        <label className="check"><input type="checkbox" checked={draft.recording_enabled} onChange={event => tab.change({ recording_enabled: event.target.checked })} /> Record this camera</label>
        <label className="field"><span>Mode when no schedule applies</span>
          <select value={draft.recording_mode} onChange={event => tab.change({ recording_mode: event.target.value as ScheduleMode })}>
            <option value="motion">Motion</option>
            <option value="continuous">Continuous</option>
            <option value="disabled">Disabled</option>
          </select>
        </label>
        <div className="field-row">
          <label className="field"><span>Pre-roll (seconds)</span>
            <input type="number" min={0} max={120} value={draft.pre_roll} onChange={event => tab.change({ pre_roll: Number(event.target.value) })} />
          </label>
          <label className="field"><span>Post-roll (seconds)</span>
            <input type="number" min={1} max={600} value={draft.post_roll} onChange={event => tab.change({ post_roll: Number(event.target.value) })} />
          </label>
        </div>
      </fieldset>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Audio</legend>
        <p className="field-hint">{AUDIO_LABEL[audioStatus] ?? audioStatus}</p>
        <label className="check">
          <input type="checkbox" checked={draft.audio_enabled} disabled={camera.audio_supported !== true}
                 onChange={event => tab.change({ audio_enabled: event.target.checked })} /> Record and allow listening to audio
        </label>
        {admin ? (
          <div className="field-actions">
            <button type="button" className="button" onClick={() => void runProbe()} disabled={probe.busy}>{probe.busy ? "Checking…" : "Check for audio"}</button>
            {probe.text ? <span role={probe.tone === "bad" ? "alert" : "status"} className={probe.tone === "bad" ? "field-error" : "test-ok"}>{probe.text}</span> : null}
          </div>
        ) : null}
      </fieldset>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Policy group</legend>
        <label className="field"><span>Group</span>
          <select value={draft.group} onChange={event => tab.change({ group: event.target.value })}>
            <option value="">No group</option>
            {(groups.data ?? []).map(group => <option key={group.id} value={group.name}>{group.name}</option>)}
            {draft.group && !(groups.data ?? []).some(group => group.name === draft.group) ? <option value={draft.group}>{draft.group}</option> : null}
          </select>
        </label>
        <p className="muted field-hint">The group supplies inherited schedules, retention, storage volume and AI settings.</p>
      </fieldset>
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} /> : null}
    </form>
  );
}
