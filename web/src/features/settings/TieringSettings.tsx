import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface Tier { id: string; name: string; kind: "local" | "s3"; local_path: string | null; s3_endpoint: string | null; s3_bucket: string | null; s3_prefix: string; has_credentials: boolean; last_checked_at: string | null; last_error: string | null; segments: number; bytes: number }
interface Policy { volume_id: string; tier_id: string; after_days: number; rate_kbps: number; enabled: boolean }
interface Tiering { tiers: Tier[]; policies: Policy[]; volumes: string[]; moves_in_progress: number; cache: { bytes: number; limit_bytes: number } }

const KEY = ["admin", "tiering"] as const;
const size = (bytes: number) => (bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`);
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
const BLANK = { name: "", kind: "local" as Tier["kind"], local_path: "", s3_endpoint: "", s3_bucket: "", s3_region: "us-east-1", s3_prefix: "", access_key: "", secret_key: "", allow_private: false };

/** Older footage moves to a cold tier (a folder, or an S3-compatible bucket) and stays playable. */
export function TieringSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<Tiering>("/api/admin/tiering", { signal }), refetchInterval: 15_000 });
  const [form, setForm] = useState(BLANK);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { tier_id: string; after_days: number; rate_mbps: number; enabled: boolean }>>({});
  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const fail = (problem: unknown, fallback: string) => { setMessage(null); setError(failureText(problem, fallback)); };
  const done = (text: string) => { setError(null); setMessage(text); };

  const add = useMutation({
    mutationFn: () => api("/api/admin/tiering/tiers", {
      method: "POST", body: form.kind === "local"
        ? { name: form.name, kind: "local", local_path: form.local_path }
        : { name: form.name, kind: "s3", s3_endpoint: form.s3_endpoint, s3_bucket: form.s3_bucket, s3_region: form.s3_region, s3_prefix: form.s3_prefix, access_key: form.access_key, secret_key: form.secret_key, allow_private: form.allow_private },
    }),
    onSuccess: async () => { setForm(BLANK); done("Cold tier added: it was written to, read from and cleaned up before it was kept."); await refresh(); },
    onError: problem => fail(problem, "The cold tier could not be added."),
  });
  const check = useMutation({ mutationFn: (id: string) => api(`/api/admin/tiering/tiers/${id}/check`, { method: "POST" }), onSuccess: async () => { done("The tier works."); await refresh(); }, onError: problem => fail(problem, "The tier could not be checked.") });
  const remove = useMutation({ mutationFn: (id: string) => api(`/api/admin/tiering/tiers/${id}`, { method: "DELETE" }), onSuccess: async () => { done("Cold tier removed."); await refresh(); }, onError: problem => fail(problem, "The tier could not be removed.") });
  const save = useMutation({
    mutationFn: ({ volume, draft }: { volume: string; draft: { tier_id: string; after_days: number; rate_mbps: number; enabled: boolean } }) =>
      api(`/api/admin/tiering/policies/${volume}`, { method: "PUT", body: { tier_id: draft.tier_id, after_days: draft.after_days, rate_kbps: Math.round(draft.rate_mbps * 1024), enabled: draft.enabled } }),
    onSuccess: async () => { done("Saved."); await refresh(); },
    onError: problem => fail(problem, "The policy could not be saved."),
  });
  const stop = useMutation({ mutationFn: (volume: string) => api(`/api/admin/tiering/policies/${volume}`, { method: "DELETE" }), onSuccess: async () => { done("Policy removed: nothing more moves from this volume."); await refresh(); }, onError: problem => fail(problem, "The policy could not be removed.") });
  const run = useMutation({
    mutationFn: () => api<{ moved: number }>("/api/admin/tiering/run", { method: "POST" }),
    onSuccess: async result => { done(`${result.moved} segment${result.moved === 1 ? "" : "s"} moved.`); await refresh(); },
    onError: problem => fail(problem, "The move could not be run."),
  });

  const data = state.data;
  const draftOf = (volume: string) => {
    const saved = data?.policies.find(policy => policy.volume_id === volume);
    return drafts[volume] ?? { tier_id: saved?.tier_id ?? data?.tiers[0]?.id ?? "", after_days: saved?.after_days ?? 30, rate_mbps: (saved?.rate_kbps ?? 20480) / 1024, enabled: saved?.enabled ?? true };
  };
  const change = (volume: string, patch: Partial<ReturnType<typeof draftOf>>) => setDrafts(current => ({ ...current, [volume]: { ...draftOf(volume), ...patch } }));
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Cold tiers">
        <h2 className="field-heading">Cold tiers</h2>
        <p className="muted field-hint">
          A cold tier is a folder (another disk, a network share mounted into the container) or an S3-compatible bucket (AWS S3, MinIO, Backblaze B2, Wasabi...). Footage moved there stays in the timeline and plays back through this NVR;
          holds, exports, verification and retention work across tiers. A bucket's keys are stored encrypted and never shown again. Footage on a bucket is fetched into a cache when it is played, so its first seconds are slower.
        </p>
        <ul className="status-list" aria-label="Tier list">
          {(data?.tiers ?? []).map(tier => (
            <li key={tier.id}>
              <strong>{tier.name}</strong>
              <span className="muted">
                {tier.kind === "local" ? `Folder ${tier.local_path}` : `Bucket ${tier.s3_bucket} at ${tier.s3_endpoint}${tier.s3_prefix ? ` (${tier.s3_prefix})` : ""}`} · {tier.segments} segments, {size(tier.bytes)} · last checked {when(tier.last_checked_at)}
                {tier.last_error ? ` · ${tier.last_error}` : ""}
              </span>
              <span className="field-actions">
                <button type="button" className="button" onClick={() => check.mutate(tier.id)}>Check {tier.name}</button>
                <button type="button" className="button" data-variant="ghost" onClick={() => remove.mutate(tier.id)}>Remove {tier.name}</button>
              </span>
            </li>
          ))}
          {data?.tiers.length === 0 ? <li className="muted">No cold tiers yet.</li> : null}
        </ul>
        <form className="tab-form" aria-label="Add a cold tier" noValidate onSubmit={event => { event.preventDefault(); add.mutate(); }}>
          <h3 className="field-heading">Add a cold tier</h3>
          <div className="field-row">
            <label className="field"><span>Tier name</span><input value={form.name} maxLength={80} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
            <label className="field"><span>Kind of tier</span>
              <select value={form.kind} onChange={event => setForm({ ...form, kind: event.target.value as Tier["kind"] })}><option value="local">A folder</option><option value="s3">An S3-compatible bucket</option></select>
            </label>
          </div>
          {form.kind === "local" ? (
            <label className="field"><span>Folder (absolute path, outside the recordings folder)</span><input value={form.local_path} placeholder="/cold" onChange={event => setForm({ ...form, local_path: event.target.value })} /></label>
          ) : (
            <>
              <div className="field-row">
                <label className="field"><span>Object store address</span><input value={form.s3_endpoint} placeholder="https://s3.eu-west-1.amazonaws.com" onChange={event => setForm({ ...form, s3_endpoint: event.target.value })} /></label>
                <label className="field"><span>Bucket</span><input value={form.s3_bucket} onChange={event => setForm({ ...form, s3_bucket: event.target.value })} /></label>
                <label className="field"><span>Region</span><input value={form.s3_region} onChange={event => setForm({ ...form, s3_region: event.target.value })} /></label>
                <label className="field"><span>Prefix inside the bucket (optional)</span><input value={form.s3_prefix} placeholder="nvr" onChange={event => setForm({ ...form, s3_prefix: event.target.value })} /></label>
              </div>
              <div className="field-row">
                <label className="field"><span>Access key</span><input autoComplete="off" value={form.access_key} onChange={event => setForm({ ...form, access_key: event.target.value })} /></label>
                <label className="field"><span>Secret key</span><input type="password" autoComplete="off" value={form.secret_key} onChange={event => setForm({ ...form, secret_key: event.target.value })} /></label>
              </div>
              <label className="check"><input type="checkbox" checked={form.allow_private} onChange={event => setForm({ ...form, allow_private: event.target.checked })} /> The object store is on a private network address</label>
            </>
          )}
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={add.isPending || !form.name.trim()}>{add.isPending ? "Testing…" : "Add cold tier"}</button></div>
        </form>
      </section>

      <section className="field-group settings-block" aria-label="Tiering policies">
        <h2 className="field-heading">When footage moves</h2>
        <p className="muted field-hint">
          Each storage volume sends footage older than the age you set to one tier, at most the rate you set. Footage that is held, or in an export that is queued or running, is not moved until it is released. A move copies the footage, reads the copy back and
          checks it against the original (and against the signed footage chain) before the hot copy is deleted, and resumes after an interruption.
        </p>
        {(data?.volumes ?? []).map(volume => {
          const saved = data?.policies.find(policy => policy.volume_id === volume);
          const draft = draftOf(volume);
          return (
            <form key={volume} className="tab-form" aria-label={`Policy for ${volume}`} noValidate onSubmit={event => { event.preventDefault(); save.mutate({ volume, draft }); }}>
              <h3 className="field-heading">Volume {volume}</h3>
              <div className="field-row">
                <label className="field"><span>Move to ({volume})</span>
                  <select value={draft.tier_id} onChange={event => change(volume, { tier_id: event.target.value })}>
                    {(data?.tiers ?? []).map(tier => <option key={tier.id} value={tier.id}>{tier.name}</option>)}
                  </select>
                </label>
                <label className="field"><span>Footage older than (days) for {volume}</span><input type="number" min={1} value={Number.isNaN(draft.after_days) ? "" : draft.after_days} onChange={event => change(volume, { after_days: event.target.value === "" ? Number.NaN : Number(event.target.value) })} /></label>
                <label className="field"><span>Rate limit (MB per second) for {volume}</span><input type="number" min={0.1} step={0.5} value={Number.isNaN(draft.rate_mbps) ? "" : draft.rate_mbps} onChange={event => change(volume, { rate_mbps: event.target.value === "" ? Number.NaN : Number(event.target.value) })} /></label>
              </div>
              <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => change(volume, { enabled: event.target.checked })} /> Move footage from {volume}</label>
              <div className="field-actions">
                <button type="submit" className="button" data-variant="primary" disabled={save.isPending || !draft.tier_id}>Save policy for {volume}</button>
                {saved ? <button type="button" className="button" data-variant="ghost" onClick={() => stop.mutate(volume)}>Remove policy for {volume}</button> : null}
              </div>
            </form>
          );
        })}
        <div className="field-actions">
          <button type="button" className="button" onClick={() => run.mutate()} disabled={run.isPending || !data?.policies.length}>{run.isPending ? "Moving…" : "Move some footage now"}</button>
        </div>
        {data ? <p className="muted">{data.moves_in_progress} move{data.moves_in_progress === 1 ? "" : "s"} in progress · cache of footage fetched back from buckets {size(data.cache.bytes)} of {size(data.cache.limit_bytes)}</p> : null}
      </section>
    </>
  );
}
