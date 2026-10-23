import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { viewKeys } from "@/api/queries";
import type { Camera, CameraGroup } from "@/api/types";
import { Modal } from "@/components/Modal";
import { ScheduleEditor, scheduleProblem } from "@/features/admin/cameras/ScheduleEditor";
import { ZoneEditor } from "@/features/admin/cameras/ZoneEditor";
import type { ScheduleInterval, Zone } from "@/features/admin/cameras/api";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft, type SaveResult } from "../tabDraft";
import {
  deleteGroup, deleteGroupRetention, deleteGroupSchedule, getGroupAi, getGroupRetention, getGroupSchedule, getGroupStorage,
  putGroupAi, putGroupRetention, putGroupSchedule, putGroupStorage, renameGroup, setMembers,
} from "./api";

interface SectionProps {
  group: CameraGroup;
  cameras: Camera[];
  admin: boolean;
}

const invalidate = (client: ReturnType<typeof useQueryClient>) => {
  void client.invalidateQueries({ queryKey: viewKeys.groups });
  void client.invalidateQueries({ queryKey: ["cameras"] });
};

const ok = <T,>(base: T, text: string): SaveResult<T> => ({ ok: true, base, outcome: { tone: "good", text } });
const bad = <T,>(base: T, error: unknown, what: string): SaveResult<T> => ({ ok: false, base, outcome: { tone: "bad", text: `${what} not saved: ${failureText(error)}.` } });

/**
 * Name and membership. Policy members (assigned on each camera or in bulk) inherit this group's
 * policies; browsing members only appear in the group in Live. The two are shown separately.
 */
export function GeneralSection({ group, cameras, admin, onDeleted }: SectionProps & { onDeleted: () => void }) {
  const client = useQueryClient();
  const policyMembers = cameras.filter(camera => camera.group_name === group.name);
  const policyIds = new Set(policyMembers.map(camera => camera.id));
  const browsing = useMemo(() => group.camera_ids.filter(id => !policyIds.has(id)).sort(), [group.camera_ids, policyMembers.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const published = useMemo(() => ({ name: group.name, browsing }), [group.name, browsing]);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const tab = useTabDraft(published, {
    label: `Name and members of ${group.name}`,
    editable: admin,
    save: async (draft, base) => {
      const saved: string[] = [];
      const failed: string[] = [];
      let next = base;
      if (draft.name.trim() !== base.name) {
        try {
          const renamed = await renameGroup(group.id, draft.name.trim());
          next = { ...next, name: renamed.name };
          saved.push("name");
        } catch (error) {
          failed.push(`Name not saved: ${failureText(error)}.`);
        }
      }
      if (JSON.stringify([...draft.browsing].sort()) !== JSON.stringify(base.browsing)) {
        try {
          await setMembers(group.id, draft.browsing);
          next = { ...next, browsing: [...draft.browsing].sort() };
          saved.push("browsing members");
        } catch (error) {
          failed.push(`Browsing members not saved: ${failureText(error)}.`);
        }
      }
      invalidate(client);
      if (failed.length) return { ok: false, base: next, outcome: { tone: saved.length ? "warn" : "bad", text: `${saved.length ? `Saved ${saved.join(" and ")}. ` : ""}${failed.join(" ")}` } };
      return ok(next, `Saved ${saved.join(" and ")}.`);
    },
  });
  const draft = tab.draft!;
  const others = cameras.filter(camera => !policyIds.has(camera.id) && !camera.archived_at);

  async function remove() {
    setDeleteError(null);
    try {
      await deleteGroup(group.id);
      setDeleting(false);
      invalidate(client);
      onDeleted();
    } catch (error) {
      setDeleteError(failureText(error));
    }
  }

  return (
    <form className="tab-form" aria-label="Group name and members" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Group</legend>
        <label className="field"><span>Name</span><input value={draft.name} maxLength={80} onChange={event => tab.change({ name: event.target.value })} /></label>
      </fieldset>
      <section className="field-group" aria-label="Policy members">
        <h3 className="field-heading">Policy members ({policyMembers.length})</h3>
        <p className="muted field-hint">These cameras inherit this group's schedule, retention, storage and AI settings unless they override them. Change a camera's policy group on its Recording tab or with a bulk change.</p>
        {policyMembers.length ? (
          <ul className="plain-list">{policyMembers.map(camera => <li key={camera.id}>{camera.name}{camera.recording_policy_source === "camera" ? <span className="muted"> · has its own schedule</span> : null}</li>)}</ul>
        ) : <p className="muted">No cameras use this group's policies.</p>}
      </section>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Also show in this group in Live</legend>
        <p className="muted field-hint">Browsing members appear when the group is opened in Live. They do not inherit its policies.</p>
        <ul className="choice-list member-list">
          {others.map(camera => (
            <li key={camera.id}>
              <label><input type="checkbox" checked={draft.browsing.includes(camera.id)}
                            onChange={event => tab.change({ browsing: event.target.checked ? [...draft.browsing, camera.id] : draft.browsing.filter(id => id !== camera.id) })} />
                {camera.name}</label>
            </li>
          ))}
        </ul>
      </fieldset>
      {admin ? (
        <>
          <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={draft.name.trim() ? null : "The group needs a name."} />
          <div className="field-actions">
            <button type="button" className="button" data-variant="danger" onClick={() => setDeleting(true)}>Delete group…</button>
          </div>
          <Modal open={deleting} onOpenChange={setDeleting} title={`Delete ${group.name}?`}
                 description="The group's schedule and retention policies are removed. Groups with policy members cannot be deleted until those cameras move."
                 footer={<><button type="button" className="button" onClick={() => setDeleting(false)}>Cancel</button><button type="button" className="button" data-variant="danger" onClick={() => void remove()}>Delete group</button></>}>
            {deleteError ? <p className="notice" data-tone="bad" role="alert">{deleteError}</p> : null}
          </Modal>
        </>
      ) : null}
    </form>
  );
}

export function ScheduleSection({ group, admin }: SectionProps) {
  const client = useQueryClient();
  const key = ["camera-groups", group.id, "schedule"] as const;
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => getGroupSchedule(group.id, signal) });
  const published = useMemo(() => (query.data ? { configured: query.data.configured, timezone: query.data.configured ? query.data.timezone : "", intervals: query.data.configured ? query.data.intervals : [] as ScheduleInterval[] } : undefined), [query.data]);
  const tab = useTabDraft(published, {
    label: `Schedule for ${group.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        if (draft.configured) await putGroupSchedule(group.id, { timezone: draft.timezone.trim(), intervals: draft.intervals });
        else await deleteGroupSchedule(group.id);
        const fresh = await getGroupSchedule(group.id);
        client.setQueryData(key, fresh);
        invalidate(client);
        return ok({ configured: fresh.configured, timezone: fresh.configured ? fresh.timezone : "", intervals: fresh.configured ? fresh.intervals : [] }, "Saved schedule. Member cameras without their own schedule follow it.");
      } catch (error) {
        return bad(base, error, "Schedule");
      }
    },
  });
  if (!tab.draft) return <p className="muted">Loading schedule…</p>;
  const draft = tab.draft;
  const problem = draft.configured ? (!draft.timezone.trim() ? "Choose the schedule's time zone." : scheduleProblem(draft.intervals)) : null;
  return (
    <form className="tab-form tab-form-wide" aria-label="Group schedule" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Weekly schedule</legend>
        <label className="check"><input type="checkbox" checked={draft.configured}
               onChange={event => tab.change({ configured: event.target.checked, timezone: draft.timezone || "UTC" })} /> Members follow a weekly schedule</label>
        {draft.configured ? (
          <>
            <label className="field"><span>Time zone</span><input value={draft.timezone} onChange={event => tab.change({ timezone: event.target.value })} autoComplete="off" /></label>
            <ScheduleEditor intervals={draft.intervals} onChange={intervals => tab.change({ intervals })} readOnly={!admin} />
          </>
        ) : <p className="muted field-hint">Without a schedule, members use their own recording mode.</p>}
      </fieldset>
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={problem} /> : null}
    </form>
  );
}

export function RetentionSection({ group, admin }: SectionProps) {
  const client = useQueryClient();
  const key = ["camera-groups", group.id, "retention"] as const;
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => getGroupRetention(group.id, signal) });
  const published = useMemo(() => (query.data ? { configured: query.data.configured, days: query.data.retention_days } : undefined), [query.data]);
  const tab = useTabDraft(published, {
    label: `Retention for ${group.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        if (draft.configured) await putGroupRetention(group.id, draft.days);
        else await deleteGroupRetention(group.id);
        const fresh = await getGroupRetention(group.id);
        client.setQueryData(key, fresh);
        invalidate(client);
        return ok({ configured: fresh.configured, days: fresh.retention_days }, "Saved retention. Member cameras without their own retention follow it.");
      } catch (error) {
        return bad(base, error, "Retention");
      }
    },
  });
  if (!tab.draft) return <p className="muted">Loading retention…</p>;
  const draft = tab.draft;
  return (
    <form className="tab-form" aria-label="Group retention" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Retention</legend>
        <label className="check"><input type="checkbox" checked={draft.configured} onChange={event => tab.change({ configured: event.target.checked })} /> Keep members' footage for a specific time</label>
        <label className="field"><span>Days to keep footage</span>
          <input type="number" min={1} max={3650} value={draft.days} disabled={!draft.configured} onChange={event => tab.change({ days: Number(event.target.value) })} />
        </label>
        {!draft.configured ? <p className="muted field-hint">Without a group retention, members use the system default.</p> : null}
      </fieldset>
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert}
                          blocked={draft.configured && !(draft.days >= 1 && draft.days <= 3650) ? "Keep footage for 1 to 3650 days." : null} /> : null}
    </form>
  );
}

export function StorageSection({ group, admin }: SectionProps) {
  const client = useQueryClient();
  const key = ["camera-groups", group.id, "storage"] as const;
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => getGroupStorage(group.id, signal), enabled: admin });
  const published = useMemo(() => (query.data ? { volume: query.data.volume_id } : undefined), [query.data]);
  const tab = useTabDraft(published, {
    label: `Storage for ${group.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const fresh = await putGroupStorage(group.id, draft.volume === "default" ? null : draft.volume);
        client.setQueryData(key, fresh);
        invalidate(client);
        return ok({ volume: fresh.volume_id }, "Saved storage volume. Member cameras record there from now on.");
      } catch (error) {
        return bad(base, error, "Storage volume");
      }
    },
  });
  if (!admin) return <p className="muted">Storage assignments are visible to administrators.</p>;
  if (!tab.draft || !query.data) return <p className="muted">Loading storage…</p>;
  return (
    <form className="tab-form" aria-label="Group storage" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <fieldset className="field-group">
        <legend>Storage volume</legend>
        <label className="field"><span>Record members' footage to</span>
          <select value={tab.draft.volume} onChange={event => tab.change({ volume: event.target.value })}>
            {query.data.configured_volumes.map(volume => <option key={volume} value={volume}>{volume}</option>)}
          </select>
        </label>
        <p className="muted field-hint">Existing footage stays where it was recorded.</p>
      </fieldset>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} />
    </form>
  );
}

export function AiSection({ group, cameras, admin }: SectionProps) {
  const client = useQueryClient();
  const key = ["camera-groups", group.id, "ai"] as const;
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => getGroupAi(group.id, signal) });
  const published = useMemo(() => (query.data ? {
    enabled: query.data.enabled, analysis_fps: query.data.analysis_fps, confidence_threshold: query.data.confidence_threshold,
    use_substream: query.data.use_substream, zones: query.data.zones as Zone[],
  } : undefined), [query.data]);
  const tab = useTabDraft(published, {
    label: `AI for ${group.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const fresh = await putGroupAi(group.id, draft);
        client.setQueryData(key, fresh);
        invalidate(client);
        return ok({ enabled: fresh.enabled, analysis_fps: fresh.analysis_fps, confidence_threshold: fresh.confidence_threshold, use_substream: fresh.use_substream, zones: fresh.zones }, "Saved AI settings.");
      } catch (error) {
        return bad(base, error, "AI settings");
      }
    },
  });
  if (!tab.draft || !query.data) return <p className="muted">Loading AI settings…</p>;
  const draft = tab.draft;
  const sample = cameras.find(camera => camera.group_name === group.name)?.id ?? null;
  return (
    <form className="tab-form" aria-label="Group AI settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice">{query.data.inherited_camera_count} of {query.data.camera_count} member cameras use these settings; the rest override them.</p>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Detection</legend>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => tab.change({ enabled: event.target.checked })} /> Detect people and vehicles</label>
        <div className="field-row">
          <label className="field"><span>Frames analysed per second</span><input type="number" min={0.1} max={5} step={0.1} value={draft.analysis_fps} onChange={event => tab.change({ analysis_fps: Number(event.target.value) })} /></label>
          <label className="field"><span>Confidence threshold</span><input type="number" min={0.1} max={0.99} step={0.01} value={draft.confidence_threshold} onChange={event => tab.change({ confidence_threshold: Number(event.target.value) })} /></label>
        </div>
        <label className="check"><input type="checkbox" checked={draft.use_substream} onChange={event => tab.change({ use_substream: event.target.checked })} /> Analyse the lower-resolution substream</label>
      </fieldset>
      <ZoneEditor cameraId={sample} zones={draft.zones} onChange={zones => tab.change({ zones })} label="Group AI zones" readOnly={!admin} />
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} /> : null}
    </form>
  );
}
