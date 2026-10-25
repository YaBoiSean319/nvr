import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { useCameras, useCreateExport, useDisplaySettings, useMe } from "@/api/queries";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";
import { usePlaybackStore } from "@/features/playback/store";
import { useStatusPanel } from "@/features/status/StatusArea";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { NotificationSettings } from "./NotificationSettings";
import {
  ACTION_LABEL, FILTER_LABEL, STATE_LABEL, useAlarm, useAlarmAction, useAlarms, useAssignees,
  type Alarm, type AlarmFilter, type AlarmQuery,
} from "./api";

const LABEL = { person: "Person", vehicle: "Vehicle" } as const;

function useWhen() {
  const display = useDisplaySettings();
  return (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
}

/** The alarm inbox: filters, a list newest first, and one alarm's handling. */
export function AlarmsInbox({ onNavigate, initialAlarmId = null }: { onNavigate: () => void; initialAlarmId?: string | null }) {
  const [query, setQuery] = useState<AlarmQuery>({ state: "open", camera_id: "", rule_id: "" });
  const [selectedId, setSelectedId] = useState<string | null>(initialAlarmId);
  const alarms = useAlarms(query);
  const cameras = useCameras();
  const when = useWhen();

  if (selectedId) return <AlarmDetailView id={selectedId} onBack={() => setSelectedId(null)} onNavigate={onNavigate} />;
  const counts = alarms.data?.counts;
  return (
    <div className="alarm-inbox">
      <form className="alarm-filters" aria-label="Alarm filters" onSubmit={event => event.preventDefault()}>
        <label className="field"><span>State</span>
          <select value={query.state} onChange={event => setQuery({ ...query, state: event.target.value as AlarmFilter })}>
            {(Object.keys(FILTER_LABEL) as AlarmFilter[]).map(state => <option key={state} value={state}>{FILTER_LABEL[state]}</option>)}
          </select>
        </label>
        <label className="field"><span>Camera</span>
          <select value={query.camera_id} onChange={event => setQuery({ ...query, camera_id: event.target.value })}>
            <option value="">All cameras</option>
            {(cameras.data ?? []).map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}
          </select>
        </label>
        <label className="field"><span>Rule</span>
          <select value={query.rule_id} onChange={event => setQuery({ ...query, rule_id: event.target.value })}>
            <option value="">All rules</option>
            {(alarms.data?.rules ?? []).map(rule => <option key={rule.id} value={rule.id}>{rule.name}</option>)}
          </select>
        </label>
      </form>
      {counts ? <p className="muted">{counts.new} new · {counts.acknowledged} acknowledged · {counts.assigned} assigned</p> : null}
      <NotificationSettings />
      {alarms.error ? <p className="notice" data-tone="bad" role="alert">{failureText(alarms.error, "Alarms could not be loaded.")}</p> : null}
      {alarms.isPending ? <p className="muted">Loading alarms…</p> : null}
      {alarms.data && !alarms.data.alarms.length ? <p className="muted">No alarms match these filters.</p> : null}
      <ul className="status-list alarm-list" aria-label="Alarms">
        {(alarms.data?.alarms ?? []).map(alarm => (
          <li key={alarm.id} data-state={alarm.state}>
            <button type="button" className="row-select alarm-row" onClick={() => setSelectedId(alarm.id)}>
              <span>{alarm.rule_name}: {LABEL[alarm.label]} on {alarm.camera_name ?? "a removed camera"}</span>
              <span className="muted">{STATE_LABEL[alarm.state]}{alarm.assigned_to ? ` to ${alarm.assigned_to}` : ""} · {when(alarm.occurred_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AlarmDetailView({ id, onBack, onNavigate }: { id: string; onBack: () => void; onNavigate: () => void }) {
  const me = useMe();
  const alarm = useAlarm(id);
  const action = useAlarmAction(id);
  const exporting = useCreateExport();
  const assignees = useAssignees(true);
  const navigate = useNavigate();
  const when = useWhen();
  const [comment, setComment] = useState("");
  const [note, setNote] = useState("");
  const [assignee, setAssignee] = useState("");
  const [deleting, setDeleting] = useState(false);
  const admin = me.data?.role === "administrator";

  if (alarm.isPending) return <p className="muted">Loading alarm…</p>;
  if (alarm.error || !alarm.data) {
    return (
      <div className="alarm-detail">
        <button type="button" className="button" onClick={onBack}><span aria-hidden="true">← </span>All alarms</button>
        <p className="notice" data-tone="bad" role="alert">{failureText(alarm.error, "The alarm could not be loaded.")}</p>
      </div>
    );
  }
  const data = alarm.data;
  const open = data.state !== "resolved";
  const run = (next: Parameters<typeof action.mutate>[0], after?: () => void) => {
    action.reset();
    action.mutate(next, { onSuccess: () => after?.() });
  };

  function addComment(event: FormEvent) {
    event.preventDefault();
    if (comment.trim()) run({ kind: "comment", body: comment.trim() }, () => setComment(""));
  }

  function openInPlayback(target: Alarm) {
    usePlaybackStore.getState().openInPlayback(target.camera_id, Date.parse(target.occurred_at));
    onNavigate();
    navigate("/playback");
  }

  // The quick clip export people use away from the desk: this camera, the alarm's clip, one file.
  function exportClip() {
    exporting.mutate({
      camera_ids: [data.camera_id], start: data.clip.starts_at, end: data.clip.ends_at, output_modes: ["individual"],
      tile_camera_ids: [], grid_resolution: "1080p", include_audio: false, audio_camera_id: null,
    });
  }

  function showJobs() {
    onNavigate();
    useStatusPanel.getState().show("jobs");
  }

  const first = data.clip.segments[0];
  const offset = first ? Math.max(0, (Date.parse(data.clip.starts_at) - Date.parse(first.starts_at)) / 1000) : 0;
  return (
    <article className="alarm-detail" aria-label={`Alarm: ${data.rule_name}`}>
      <button type="button" className="button" onClick={onBack}><span aria-hidden="true">← </span>All alarms</button>
      <header>
        <h3>{data.rule_name}: {LABEL[data.label]} on {data.camera_name ?? "a removed camera"}</h3>
        <p className="muted">{when(data.occurred_at)} · <span data-state={data.state}>{STATE_LABEL[data.state]}{data.assigned_to ? ` to ${data.assigned_to}` : ""}</span></p>
      </header>
      {data.snapshot_url ? <img className="alarm-snapshot" src={data.snapshot_url} alt={`Snapshot from ${data.camera_name ?? "the camera"} at the alarm time`} /> : null}
      {first ? (
        <video className="alarm-clip" controls muted preload="metadata" src={`${first.media_url}#t=${offset.toFixed(1)}`} aria-label="Alarm clip" />
      ) : <p className="muted">{data.clip.note}</p>}
      <div className="field-actions">
        <button type="button" className="button" onClick={() => openInPlayback(data)}>Open in Playback</button>
        {data.state === "new" ? <button type="button" className="button" data-variant="primary" disabled={action.isPending} onClick={() => run({ kind: "acknowledge" })}>Acknowledge</button> : null}
        {first ? <button type="button" className="button" disabled={exporting.isPending || exporting.isSuccess} onClick={exportClip}>Export clip</button> : null}
      </div>
      {exporting.isSuccess ? (
        <p className="notice" data-tone="good" role="status">Clip export queued. <button type="button" className="button" data-variant="ghost" onClick={showJobs}>View export jobs</button></p>
      ) : null}
      {exporting.error ? <p className="notice" data-tone="bad" role="alert">Clip not exported: {failureText(exporting.error)}.</p> : null}
      {action.error && !deleting ? <p className="notice" data-tone="bad" role="alert">{failureText(action.error)}</p> : null}

      {open ? (
        <form className="alarm-form" aria-label="Assign alarm" onSubmit={event => { event.preventDefault(); if (assignee) run({ kind: "assign", userId: Number(assignee) }, () => setAssignee("")); }}>
          <label className="field"><span>Assign to</span>
            <select value={assignee} onChange={event => setAssignee(event.target.value)}>
              <option value="">Choose a person</option>
              {(assignees.data ?? []).map(user => <option key={user.id} value={user.id}>{user.username}</option>)}
            </select>
          </label>
          <button type="submit" className="button" disabled={!assignee || action.isPending}>Assign</button>
        </form>
      ) : null}

      <section aria-label="Comments" className="alarm-section">
        <h4>Comments</h4>
        {data.comments.length ? (
          <ul className="plain-list">{data.comments.map(item => <li key={item.id}><strong>{item.author ?? "Removed user"}</strong> · {when(item.created_at)}<br />{item.body}</li>)}</ul>
        ) : <p className="muted">No comments yet.</p>}
        <form className="alarm-form" aria-label="Add comment" onSubmit={addComment}>
          <label className="field"><span>Comment</span><textarea value={comment} maxLength={2000} rows={2} onChange={event => setComment(event.target.value)} /></label>
          <button type="submit" className="button" disabled={!comment.trim() || action.isPending}>Add comment</button>
        </form>
      </section>

      {open ? (
        <form className="alarm-form" aria-label="Resolve alarm" onSubmit={event => { event.preventDefault(); run({ kind: "resolve", note: note.trim() }, () => setNote("")); }}>
          <label className="field"><span>Resolution note (optional)</span><input value={note} maxLength={2000} onChange={event => setNote(event.target.value)} /></label>
          <button type="submit" className="button" data-variant="primary" disabled={action.isPending}>Resolve</button>
        </form>
      ) : <p className="muted">Resolved by {data.resolved_by ?? "a removed user"} {data.resolved_at ? when(data.resolved_at) : ""}.</p>}

      <section aria-label="History" className="alarm-section">
        <h4>History</h4>
        <ol className="plain-list">
          {data.history.map(entry => (
            <li key={`${entry.occurred_at}-${entry.action}`}>{ACTION_LABEL[entry.action] ?? entry.action}{entry.actor ? ` by ${entry.actor}` : ""} · {when(entry.occurred_at)}
              {typeof entry.details.assigned_to === "string" ? ` · to ${entry.details.assigned_to}` : ""}</li>
          ))}
        </ol>
      </section>

      {admin ? (
        <>
          <div className="field-actions"><button type="button" className="button" data-variant="danger" onClick={() => setDeleting(true)}>Delete alarm…</button></div>
          <Modal open={deleting} onOpenChange={setDeleting} title="Delete this alarm?"
                 description="The alarm, its comments and history are removed. The audit log keeps a record of the deletion."
                 footer={<><button type="button" className="button" onClick={() => setDeleting(false)}>Cancel</button>
                   <button type="button" className="button" data-variant="danger" disabled={action.isPending}
                           onClick={() => run({ kind: "delete" }, () => { setDeleting(false); onBack(); })}>Delete alarm</button></>}>
            {action.error ? <p className="notice" data-tone="bad" role="alert">{failureText(action.error)}</p> : null}
          </Modal>
        </>
      ) : null}
    </article>
  );
}
