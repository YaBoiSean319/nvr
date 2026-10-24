import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { TabFooter } from "@/features/admin/TabFooter";
import { alarmKeys } from "@/features/alarms/api";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";

interface Volume {
  id: string; relative_path: string | null; state: string; free_bytes: number | null; capacity_bytes: number | null;
  write_latency_ms: number | null; retention_at_risk: boolean; cameras: { id: string; name: string }[];
}
interface Run { id: string; kind: string; status: string; created_at: string; finished_at: string | null; file_name: string | null; size_bytes: number | null; error: string | null }
interface BackupStatus {
  enabled: boolean; disabled_reason: string | null; destination: string; destination_error: string | null; keep: number;
  schedule: { local_time: string; time_zone: string }; last_success: Run | null; last_failure: Run | null; age_hours: number | null;
  stale: boolean; stored: { file_name: string; size_bytes: number }[]; runs: Run[]; footage_included: boolean;
}

const size = (bytes: number | null | undefined) => {
  if (bytes === null || bytes === undefined) return "unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
};

/** Storage volumes and their health, the retention default, and configuration backups. */
export function StorageSettings() {
  const client = useQueryClient();
  const storage = useQuery({ queryKey: ["storage"], queryFn: ({ signal }) => api<{ volumes: Volume[]; minimum_free_bytes: number }>("/api/storage", { signal }), refetchInterval: 30_000 });
  const backups = useQuery({
    queryKey: ["backups"],
    queryFn: ({ signal }) => api<BackupStatus>("/api/backups", { signal }),
    refetchInterval: query => (query.state.data?.runs.some(run => run.status === "queued" || run.status === "running") ? 2_000 : 30_000),
  });
  const [backupError, setBackupError] = useState<string | null>(null);
  const [queued, setQueued] = useState(false);

  async function backupNow() {
    setBackupError(null);
    try {
      await api("/api/backups", { method: "POST" });
      setQueued(true);
      await client.invalidateQueries({ queryKey: ["backups"] });
    } catch (error) {
      setBackupError(failureText(error));
    }
  }

  const status = backups.data;
  const active = status?.runs.find(run => run.status === "queued" || run.status === "running");
  return (
    <div className="settings-stack">
      <section className="field-group" aria-label="Storage volumes">
        <h2 className="field-heading">Storage volumes</h2>
        {storage.data ? (
          <table className="inventory-table">
            <thead><tr><th scope="col">Volume</th><th scope="col">State</th><th scope="col">Free</th><th scope="col">Write latency</th><th scope="col">Cameras</th></tr></thead>
            <tbody>
              {storage.data.volumes.map(volume => (
                <tr key={volume.id} data-state={volume.state}>
                  <td>{volume.id}{volume.relative_path ? <span className="muted mono"> · {volume.relative_path}</span> : null}</td>
                  <td>{volume.state}{volume.retention_at_risk ? <span className="field-error"> · retention at risk</span> : null}</td>
                  <td>{size(volume.free_bytes)}{volume.capacity_bytes ? ` of ${size(volume.capacity_bytes)}` : ""}</td>
                  <td>{volume.write_latency_ms === null ? "—" : `${volume.write_latency_ms} ms`}</td>
                  <td>{volume.cameras.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">{storage.error ? failureText(storage.error) : "Loading…"}</p>}
        <p className="muted field-hint">Volumes are configured on the NVR host. Assign a policy group to a volume in Groups &amp; Policies.</p>
      </section>

      <section className="field-group" aria-label="Retention default">
        <h2 className="field-heading">Retention default</h2>
        <p>Footage is kept for 30 days unless a group or camera sets its own retention. Held, playing and exporting footage is never removed early.</p>
      </section>

      <section className="field-group" aria-label="Configuration backups">
        <h2 className="field-heading">Configuration backups</h2>
        {status ? (
          <>
            {status.enabled ? (
              <p className="notice" data-tone={status.stale ? "warn" : "good"}>
                {status.last_success
                  ? `Last backup ${formatRelative(status.last_success.finished_at)} (${status.last_success.file_name}, ${size(status.last_success.size_bytes)}).`
                  : "No backup has completed yet."}
                {status.stale ? " Backups are overdue." : ""}
              </p>
            ) : <p className="notice" data-tone="warn">Backups are disabled: {status.disabled_reason}</p>}
            {status.last_failure && (!status.last_success || (status.last_failure.finished_at ?? "") > (status.last_success.finished_at ?? "")) ? (
              <p className="notice" data-tone="bad">The latest backup failed {formatRelative(status.last_failure.finished_at)}: {status.last_failure.error}</p>
            ) : null}
            <dl className="details">
              <dt>Schedule</dt><dd>Daily at {status.schedule.local_time} ({status.schedule.time_zone}), keeping {status.keep}</dd>
              <dt>Destination</dt><dd className="mono">{status.destination}{status.destination_error ? ` · ${status.destination_error}` : ""}</dd>
              <dt>Contents</dt><dd>Settings, cameras, users and policies, encrypted. Recorded footage is not included.</dd>
              <dt>Stored backups</dt><dd>{status.stored.length ? status.stored.map(file => file.file_name).join(", ") : "None"}</dd>
            </dl>
            <div className="field-actions">
              <button type="button" className="button" data-variant="primary" onClick={() => void backupNow()} disabled={!status.enabled || Boolean(active)}>
                {active ? `Backup ${active.status}…` : "Back up now"}
              </button>
              {queued && !active ? <span role="status" className="test-ok">Backup finished: {status.runs[0]?.status}{status.runs[0]?.error ? ` · ${status.runs[0].error}` : ""}.</span> : null}
              {backupError ? <span className="field-error" role="alert">{backupError}</span> : null}
            </div>
          </>
        ) : <p className="muted">{backups.error ? failureText(backups.error) : "Loading…"}</p>}
      </section>
      <AlarmFootage />
    </div>
  );
}

/** How long a resolved alarm keeps its footage beyond the camera's retention. */
function AlarmFootage() {
  const client = useQueryClient();
  const settings = useQuery({ queryKey: alarmKeys.settings, queryFn: ({ signal }) => api<{ resolved_footage_days: number }>("/api/alarm-settings", { signal }) });
  const tab = useTabDraft(settings.data, {
    label: "Alarm footage",
    editable: true,
    save: async (draft, base) => {
      try {
        const saved = await api<{ resolved_footage_days: number }>("/api/alarm-settings", { method: "PUT", body: draft });
        client.setQueryData(alarmKeys.settings, saved);
        return { ok: true, base: saved, outcome: { tone: "good", text: "Saved." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Not saved: ${failureText(error)}.` } };
      }
    },
  });
  const days = tab.draft?.resolved_footage_days;
  return (
    <form className="field-group" aria-label="Alarm footage" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <h2 className="field-heading">Alarm footage</h2>
      <p className="muted field-hint">Footage around an alarm is kept, whatever the camera's retention, until the alarm is resolved and then for this many days.</p>
      {tab.draft ? (
        <>
          <label className="field"><span>Days to keep after resolution</span>
            <input type="number" min={0} max={3650} value={Number.isNaN(days) ? "" : days}
                   onChange={event => tab.change({ resolved_footage_days: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
          </label>
          <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert}
                     blocked={days !== undefined && days >= 0 && days <= 3650 && Number.isInteger(days) ? null : "Enter a whole number of days from 0 to 3650."} />
        </>
      ) : <p className="muted">{settings.error ? failureText(settings.error) : "Loading…"}</p>}
    </form>
  );
}
