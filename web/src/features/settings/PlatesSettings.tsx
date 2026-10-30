import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";
import { KIND_LABEL, PLATES_KEY, usePlateAdmin, type WatchList } from "@/features/plates/api";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

function WatchListCard({ list, fail, done }: { list: WatchList; fail: (problem: unknown, fallback: string) => void; done: (text: string) => void }) {
  const client = useQueryClient();
  const [entry, setEntry] = useState({ pattern: "", note: "", expires: "" });
  const refresh = () => client.invalidateQueries({ queryKey: PLATES_KEY });
  const add = useMutation({
    mutationFn: () => api(`/api/admin/plates/watch-lists/${list.id}/entries`, {
      method: "POST", body: { pattern: entry.pattern, note: entry.note, expires_at: entry.expires ? new Date(entry.expires).toISOString() : null },
    }),
    onSuccess: async () => { setEntry({ pattern: "", note: "", expires: "" }); done("Plate added."); await refresh(); },
    onError: problem => fail(problem, "The plate could not be added."),
  });
  const removeEntry = useMutation({
    mutationFn: (id: string) => api(`/api/admin/plates/watch-lists/${list.id}/entries/${id}`, { method: "DELETE" }),
    onSuccess: async () => { done("Plate removed."); await refresh(); },
    onError: problem => fail(problem, "The plate could not be removed."),
  });
  const change = useMutation({
    mutationFn: (lookalikes: boolean) => api(`/api/admin/plates/watch-lists/${list.id}`, { method: "PUT", body: { name: list.name, kind: list.kind, lookalikes } }),
    onSuccess: async () => { done("Saved."); await refresh(); },
    onError: problem => fail(problem, "The list could not be saved."),
  });
  const removeList = useMutation({
    mutationFn: () => api(`/api/admin/plates/watch-lists/${list.id}`, { method: "DELETE" }),
    onSuccess: async () => { done("List removed."); await refresh(); },
    onError: problem => fail(problem, "The list could not be removed."),
  });
  return (
    <article className="field-group settings-block" aria-label={`Watch list ${list.name}`}>
      <h3 className="field-heading">{list.name}</h3>
      <p className="muted field-hint">{KIND_LABEL[list.kind]}.</p>
      <label className="check"><input type="checkbox" checked={list.lookalikes} disabled={change.isPending} onChange={event => change.mutate(event.target.checked)} /> Treat look-alike characters as the same (O, Q or D and 0, I or L and 1, S and 5, B and 8, Z and 2) in {list.name}</label>
      <ul className="status-list" aria-label={`Plates on ${list.name}`}>
        {list.entries.map(item => (
          <li key={item.id}>
            <strong>{item.pattern}</strong>
            <span className="muted">{item.note ? `${item.note} · ` : ""}{item.expires_at ? `${item.expired ? "expired" : "expires"} ${when(item.expires_at)}` : "no expiry"}</span>
            <button type="button" className="button" data-variant="ghost" onClick={() => removeEntry.mutate(item.id)}>Remove {item.pattern} from {list.name}</button>
          </li>
        ))}
        {list.entries.length === 0 ? <li className="muted">No plates on this list yet.</li> : null}
      </ul>
      <form className="tab-form" aria-label={`Add a plate to ${list.name}`} onSubmit={event => { event.preventDefault(); add.mutate(); }}>
        <div className="field-row">
          <label className="field"><span>Plate for {list.name}</span><input value={entry.pattern} maxLength={16} placeholder="AB12CDE or AB1*" onChange={event => setEntry({ ...entry, pattern: event.target.value })} /></label>
          <label className="field"><span>Note for {list.name}</span><input value={entry.note} maxLength={200} onChange={event => setEntry({ ...entry, note: event.target.value })} /></label>
          <label className="field"><span>Expires (optional) for {list.name}</span><input type="datetime-local" value={entry.expires} onChange={event => setEntry({ ...entry, expires: event.target.value })} /></label>
        </div>
        <div className="field-actions">
          <button type="submit" className="button" data-variant="primary" disabled={add.isPending || entry.pattern.trim().length < 2}>Add plate to {list.name}</button>
          <button type="button" className="button" data-variant="ghost" onClick={() => removeList.mutate()}>Delete {list.name}</button>
        </div>
      </form>
    </article>
  );
}

/** Which cameras read licence plates, and the allow and deny lists that rules use. */
export function PlatesSettings() {
  const client = useQueryClient();
  const state = usePlateAdmin();
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", kind: "deny" as WatchList["kind"], lookalikes: false });
  const refresh = () => client.invalidateQueries({ queryKey: PLATES_KEY });
  const fail = (problem: unknown, fallback: string) => { setMessage(null); setError(failureText(problem, fallback)); };
  const done = (text: string) => { setError(null); setMessage(text); };

  const camera = useMutation({
    mutationFn: ({ id, enabled, min }: { id: string; enabled: boolean; min: number }) => api(`/api/admin/plates/cameras/${id}`, { method: "PUT", body: { enabled, min_confidence: min } }),
    onSuccess: async () => { done("Saved."); await refresh(); },
    onError: problem => fail(problem, "The camera could not be saved."),
  });
  const chooseRegion = useMutation({
    mutationFn: (region: string) => api("/api/admin/plates/region", { method: "PUT", body: { region } }),
    onSuccess: async () => { done("Region saved. Plate reading uses it from the next picture, with no restart."); await refresh(); },
    onError: problem => fail(problem, "The region could not be saved."),
  });
  const create = useMutation({
    mutationFn: () => api("/api/admin/plates/watch-lists", { method: "POST", body: form }),
    onSuccess: async () => { setForm({ name: "", kind: "deny", lookalikes: false }); done("Watch list created."); await refresh(); },
    onError: problem => fail(problem, "The list could not be created."),
  });

  const data = state.data;
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Plate reading">
        <h2 className="field-heading">Plate reading</h2>
        <p className="muted field-hint">
          Cameras designated here have each vehicle they see read for a licence plate: a plate detector and recogniser run on a full-size picture, and each read is kept with its vehicle, a confidence and a picture of the plate.
          Reads follow footage retention and access grants. Recognition quality depends on the camera and the site: read the confidence, and check a read against the footage before acting on it.
          Turn on AI analysis for a camera as well, because plates are read on the vehicles analysis finds.
        </p>
        <fieldset className="field-group" aria-label="Plate region" disabled={chooseRegion.isPending}>
          <legend>Region</legend>
          <p className="muted field-hint">
            A plate reader knows only the plates it was trained on, so choose the region your cameras see. The models for the first two are built into this installation; accuracy on your own cameras is not known until you measure it
            (<code>python -m app.plates evaluate</code>; see docs/licence-plates.md).
          </p>
          {(data?.models.regions ?? []).map(region => (
            <label key={region.id} className="check" data-installed={region.installed}>
              <input type="radio" name="plate-region" value={region.id} checked={data?.models.region === region.id} disabled={!region.installed} onChange={() => chooseRegion.mutate(region.id)} />
              {" "}<strong>{region.label}</strong>
              <span className="muted"> {region.covers} {region.manual ? "" : `(${((region.size_bytes ?? 0) / 1e6).toFixed(0)} MB)`} {region.installed ? "Installed." : region.manual ? "Not installed: install the models by hand to use this." : "Not installed in this image."}</span>
            </label>
          ))}
        </fieldset>
        {data && data.models.region && !data.models.regions.find(region => region.id === data.models.region)?.installed ? (
          <p className="notice" data-tone="warn" role="status">
            The models for the chosen region are not installed, so nothing is read yet. Choose an installed region, or install the models; see docs/licence-plates.md.
          </p>
        ) : null}
        <ul className="status-list" aria-label="Cameras for plates">
          {(data?.cameras ?? []).map(item => (
            <li key={item.id}>
              <label className="check"><input type="checkbox" checked={item.enabled} disabled={camera.isPending} onChange={event => camera.mutate({ id: item.id, enabled: event.target.checked, min: item.min_confidence })} /> Read plates on {item.name}</label>
              <label className="field"><span>Lowest confidence kept for {item.name}</span>
                <select value={String(item.min_confidence)} disabled={camera.isPending} onChange={event => camera.mutate({ id: item.id, enabled: item.enabled, min: Number(event.target.value) })}>
                  {[0.4, 0.5, 0.6, 0.7, 0.8, 0.9].map(value => <option key={value} value={String(value)}>{Math.round(value * 100)}%</option>)}
                </select>
              </label>
            </li>
          ))}
          {data?.cameras.length === 0 ? <li className="muted">No cameras yet.</li> : null}
        </ul>
      </section>

      <section className="field-group settings-block" aria-label="Watch lists">
        <h2 className="field-heading">Watch lists</h2>
        <p className="muted field-hint">
          A rule can watch a list (add one under Rules, condition “A licence plate read”). A rule on a <strong>deny</strong> list raises an alarm when a plate on the list is read; a rule on an <strong>allow</strong> list raises one when a plate that is
          <em> not</em> on it is read, and nothing for a plate that is. Write a plate as it appears, or use <code>*</code> for any run of characters and <code>?</code> for one character (<code>AB12*</code>, <code>AB??CDE</code>). Entries can expire.
        </p>
        {(data?.watch_lists ?? []).map(list => <WatchListCard key={list.id} list={list} fail={fail} done={done} />)}
        {data?.watch_lists.length === 0 ? <p className="muted">No watch lists yet.</p> : null}
        <form className="tab-form" aria-label="Add a watch list" onSubmit={event => { event.preventDefault(); create.mutate(); }}>
          <h3 className="field-heading">Add a watch list</h3>
          <div className="field-row">
            <label className="field"><span>List name</span><input value={form.name} maxLength={80} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
            <label className="field"><span>Kind of list</span>
              <select value={form.kind} onChange={event => setForm({ ...form, kind: event.target.value as WatchList["kind"] })}>
                <option value="deny">{KIND_LABEL.deny}</option><option value="allow">{KIND_LABEL.allow}</option>
              </select>
            </label>
          </div>
          <label className="check"><input type="checkbox" checked={form.lookalikes} onChange={event => setForm({ ...form, lookalikes: event.target.checked })} /> Treat look-alike characters as the same</label>
          <div className="field-actions"><button type="submit" className="button" data-variant="primary" disabled={create.isPending || !form.name.trim()}>Add list</button></div>
        </form>
      </section>
    </>
  );
}
