import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface Release { version: string; tag: string; notes: string; url: string; published_at: string; image: string; schema_version: number | null; has_migrations: boolean | null }
interface Migration { version: number; name: string }
interface Updates {
  version: string; schema_version: number; image: string; check_enabled: boolean; disabled_by_environment: boolean; checking: boolean;
  last_checked_at: string | null; last_error: string | null; stale: boolean; available: Release | null; rollback: { steps: string[]; copy: string | null; previous_image: string; has_copy: boolean; will_migrate: boolean } | null;
  prepared: { at: string; run_id: string | null; without_backup: boolean } | null;
  history: { at: string; from_version: string; to_version: string; from_schema: number | null; to_schema: number; migrations: Migration[] }[];
  applied_migrations: { version: number; name: string; applied_at: string }[];
}
interface BackupRun { id: string; status: string; file_name: string | null; size_bytes: number | null; finished_at: string | null }
interface Plan {
  target: Release; backup: BackupRun | null; backup_destination: string | null; without_backup: boolean; upgrade: string[]; pre_migration_copies: { file: string; from_schema: number | null; to_schema: number | null; size_bytes: number }[];
  rollback: { steps: string[]; copy: string | null; previous_image: string; has_copy: boolean; will_migrate: boolean }; will_migrate: boolean | null; note: string;
}

const KEY = ["admin", "updates"] as const;
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

function Commands({ lines, label }: { lines: string[]; label: string }) {
  const [copied, setCopied] = useState(false);
  const text = lines.join("\n");
  return (
    <div className="command-block">
      <pre aria-label={label} tabIndex={0}>{text}</pre>
      <button type="button" className="button" onClick={() => { void navigator.clipboard?.writeText(text).then(() => setCopied(true)); }}>{copied ? "Copied" : `Copy ${label.toLowerCase()}`}</button>
    </div>
  );
}

/** Whether a newer release is out and what it changes, then the exact host commands to upgrade and to roll back. The NVR runs none of them. */
export function UpdatesSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<Updates>("/api/admin/updates", { signal }) });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const prepared = state.data?.prepared ?? null;
  const plan = useQuery({
    queryKey: [...KEY, "plan", prepared?.at] as const, enabled: Boolean(prepared),
    queryFn: ({ signal }) => api<Plan>("/api/admin/updates/plan", { signal }), retry: false,
    refetchInterval: query => (query.state.error ? 3_000 : false),
  });
  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const asked = useRef(false);
  const fail = (problem: unknown, fallback: string) => { setMessage(null); setError(failureText(problem, fallback)); };
  const setting = useMutation({
    mutationFn: (check_enabled: boolean) => api("/api/admin/updates/settings", { method: "PUT", body: { check_enabled } }),
    onSuccess: async () => { setError(null); setMessage("Saved."); await refresh(); }, onError: problem => fail(problem, "The setting could not be saved."),
  });
  const check = useMutation({
    mutationFn: () => api<Updates>("/api/admin/updates/check", { method: "POST" }),
    onSuccess: async result => { setError(null); setMessage(result.last_error ? null : result.available ? `Version ${result.available.version} is available.` : "You are up to date."); await refresh(); },
    onError: problem => fail(problem, "The check could not be made."),
  });
  const prepare = useMutation({
    mutationFn: (without_backup: boolean) => api("/api/admin/updates/prepare", { method: "POST", body: { without_backup } }),
    onSuccess: async () => { setError(null); setMessage(null); await refresh(); void plan.refetch(); }, onError: problem => fail(problem, "The update could not be started."),
  });

  const data = state.data;
  useEffect(() => {
    // Opening the page with an old (or no) check asks once, so a new release is shown without anyone pressing a button. Never when the check is off.
    if (data && data.checking && data.stale && !asked.current) { asked.current = true; check.mutate(); }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  if (state.isPending) return <p className="muted">Loading…</p>;
  if (state.error || !data) return <p className="notice" data-tone="bad" role="alert">{failureText(state.error, "The update status could not be loaded.")}</p>;
  const available = data.available;
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Version">
        <h2 className="field-heading">Updates</h2>
        <p>Running version <strong>{data.version}</strong>, database schema <strong>v{data.schema_version}</strong>.</p>
        <label className="check">
          <input type="checkbox" checked={data.check_enabled && !data.disabled_by_environment} disabled={data.disabled_by_environment || setting.isPending} onChange={event => setting.mutate(event.target.checked)} />
          {" "}Check for new releases (asks the published releases list; nothing about this installation is sent)
        </label>
        {data.disabled_by_environment ? <p className="muted field-hint">The check is turned off for this installation by NVR_UPDATE_CHECK, so no request is ever made.</p> : null}
        <div className="field-actions">
          <button type="button" className="button" disabled={!data.checking || check.isPending} onClick={() => check.mutate()}>{check.isPending ? "Checking…" : "Check now"}</button>
          <span className="muted">Last checked {when(data.last_checked_at)}</span>
        </div>
        {data.last_error ? <p className="notice" data-tone="bad" role="alert">The last check failed: {data.last_error}.</p> : null}
      </section>

      {available ? (
        <section className="field-group settings-block" aria-label="Available release">
          <h2 className="field-heading">Version {available.version} is available</h2>
          <p className="muted">
            Published {when(available.published_at)}. {available.has_migrations === null ? "The release does not say whether it changes the database."
              : available.has_migrations ? `It changes the database (schema v${data.schema_version} to v${available.schema_version}): a copy of the database is made automatically before the changes are applied.` : "It does not change the database schema."}
            {available.url ? <> <a href={available.url} target="_blank" rel="noreferrer">Release page</a></> : null}
          </p>
          <pre className="release-notes" aria-label={`Release notes for ${available.version}`}>{available.notes || "No release notes."}</pre>
          {!prepared ? (
            <div className="field-actions">
              <button type="button" className="button" data-variant="primary" disabled={prepare.isPending} onClick={() => prepare.mutate(false)}>Back up, then show the upgrade steps</button>
              <button type="button" className="button" data-variant="ghost" disabled={prepare.isPending} onClick={() => prepare.mutate(true)}>Show the steps without a backup</button>
            </div>
          ) : null}
        </section>
      ) : data.last_checked_at && !data.last_error ? <p className="muted">You are running the newest release.</p> : null}

      {prepared ? (
        <section className="field-group settings-block" aria-label="Upgrade steps">
          <h2 className="field-heading">Upgrade steps</h2>
          {plan.data ? (
            <>
              <h3 className="field-heading">1. The pre-update backup</h3>
              {plan.data.backup ? (
                <p role="status">Backed up: <strong>{plan.data.backup.file_name}</strong> in {plan.data.backup_destination}{plan.data.backup.size_bytes ? ` (${Math.round(plan.data.backup.size_bytes / 1024)} KB)` : ""}. It holds the configuration and credentials, not footage.</p>
              ) : <p className="notice" data-tone="warn" role="status">No backup was made (backups are not set up). The database copy made just before migrations is then the only way back.</p>}
              <h3 className="field-heading">2. Upgrade, on the host</h3>
              <Commands lines={plan.data.upgrade} label="Upgrade commands" />
              <h3 className="field-heading">3. If you need to go back</h3>
              <p className="muted">{plan.data.rollback.will_migrate ? "The database copy made just before this upgrade's migrations is created when the new version first starts; after the upgrade this page names it." : "This release does not change the database, so only the image goes back."}</p>
              <Commands lines={plan.data.rollback.steps} label="Rollback commands" />
              <p className="muted field-hint">{plan.data.note}</p>
            </>
          ) : (
            <p className="muted" role="status">{plan.error ? failureText(plan.error, "Waiting for the backup…") : "Waiting for the backup…"}</p>
          )}
        </section>
      ) : null}

      <section className="field-group settings-block" aria-label="Upgrade history">
        <h2 className="field-heading">After an upgrade</h2>
        {data.history.length === 0 ? <p className="muted">No upgrade has been seen yet.</p> : (
          <ul className="status-list">
            {data.history.map(item => (
              <li key={item.at}>
                <strong>{item.from_version} to {item.to_version}</strong>
                <span className="muted">{when(item.at)} · schema v{item.from_schema ?? "?"} to v{item.to_schema} · {item.migrations.length ? `migrations applied: ${item.migrations.map(migration => `${migration.version} ${migration.name}`).join(", ")}` : "no migrations"}</span>
              </li>
            ))}
          </ul>
        )}
        {data.rollback ? (
          <>
            <h3 className="field-heading">To undo the last upgrade</h3>
            <p className="muted">{data.rollback.has_copy ? <>The database copy made just before its migrations is <strong>{data.rollback.copy}</strong>.</> : data.rollback.will_migrate ? "The database copy made before its migrations is no longer on disk; use the pre-update backup." : "That upgrade did not change the database, so only the image goes back."}</p>
            <Commands lines={data.rollback.steps} label="Undo commands" />
          </>
        ) : null}
        <h3 className="field-heading">Most recent migrations</h3>
        <ul className="status-list" aria-label="Applied migrations">
          {data.applied_migrations.map(item => <li key={item.version}><strong>{item.version} {item.name}</strong><span className="muted">{when(item.applied_at)}</span></li>)}
        </ul>
      </section>
    </>
  );
}
