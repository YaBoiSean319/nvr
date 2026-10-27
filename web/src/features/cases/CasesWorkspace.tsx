import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api } from "@/api/client";
import { useDisplaySettings } from "@/api/queries";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";
import { usePlaybackStore } from "@/features/playback/store";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { addItem, caseKeys, createCase, useCase, useCaseAudit, useCases, type CaseDetail, type CaseItem, type CasePackage, type PackageCheck } from "./api";

const KIND_LABEL: Record<string, string> = { clip: "Clip", mark: "Mark", note: "Note", alarm: "Alarm", search: "Saved search", export: "Export" };

function useWhen() {
  const display = useDisplaySettings();
  return (iso: string | null | undefined) => (iso && display.data ? formatSiteDateTime(Date.parse(iso), display.data) : (iso ?? ""));
}

/** The Cases list: every case this person is in (administrators see all), and a form to start one. */
export function CasesList() {
  const cases = useCases();
  const navigate = useNavigate();
  const client = useQueryClient();
  const when = useWhen();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function start(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const created = await createCase(title, description);
      void client.invalidateQueries({ queryKey: caseKeys.all });
      navigate(`/cases/${created.id}`);
    } catch (problem) {
      setError(failureText(problem, "The case could not be created."));
    }
  }

  return (
    <section className="cases" aria-labelledby="cases-title">
      <header className="cases-head">
        <h1 id="cases-title">Cases</h1>
        <Link className="button" data-variant="ghost" to="/playback">Back to Playback</Link>
      </header>
      <form className="cases-new" onSubmit={start} aria-label="Start a case">
        <label className="field"><span>Title</span><input value={title} maxLength={200} onChange={event => setTitle(event.target.value)} required /></label>
        <label className="field"><span>What happened</span><textarea value={description} rows={2} maxLength={5000} onChange={event => setDescription(event.target.value)} /></label>
        <button type="submit" className="button" data-variant="primary" disabled={!title.trim()}>Start case</button>
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      </form>
      {cases.error ? <p className="notice" data-tone="bad" role="alert">{failureText(cases.error, "Cases could not be loaded.")}</p> : null}
      {cases.isPending ? <p className="muted">Loading cases…</p> : null}
      {cases.data && !cases.data.length ? <p className="muted">No cases yet. Start one here, or add a clip to a case from Playback.</p> : null}
      <ul className="status-list cases-list" aria-label="Cases">
        {(cases.data ?? []).map(item => (
          <li key={item.id} data-state={item.status}>
            <Link className="row-select case-row" to={`/cases/${item.id}`}>
              <span>{item.title}</span>
              <span className="muted">{item.status === "open" ? "Open" : "Closed"} · {item.owner ?? "no owner"} · {item.counts.clip} clip{item.counts.clip === 1 ? "" : "s"}, {item.counts.note} note{item.counts.note === 1 ? "" : "s"} · {when(item.created_at)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function itemSummary(item: CaseItem, when: (iso: string | null | undefined) => string): string {
  if (item.restricted) return "An item on a camera you may not see";
  const data = item.data ?? {};
  switch (item.kind) {
    case "clip": return `${item.camera_name ?? "Camera"}, ${when(item.starts_at)} to ${when(item.ends_at)}${item.text ? `: ${item.text}` : ""}`;
    case "mark": return `${item.camera_name ?? "Camera"} at ${when(item.starts_at)}: ${item.text}`;
    case "alarm": return `${item.text} on ${item.camera_name ?? "a camera"} at ${when(item.starts_at)}`;
    case "search": return `${item.text}: ${(data.track_ids as string[] | undefined)?.length ?? 0} found`;
    case "export": return `Export of ${when(item.starts_at)} to ${when(item.ends_at)}`;
    default: return item.text ?? "";
  }
}

/** One case: its items, members, holds, notes and evidence package. */
export function CaseView() {
  const { caseId = "" } = useParams();
  const detail = useCase(caseId);
  if (detail.error) {
    return (
      <section className="cases"><p className="notice" data-tone="bad" role="alert">{failureText(detail.error, "This case could not be opened.")}</p><Link className="button" to="/cases">All cases</Link></section>
    );
  }
  if (!detail.data) return <section className="cases"><p className="muted">Loading case…</p></section>;
  return <OpenCase value={detail.data} />;
}

function OpenCase({ value }: { value: CaseDetail }) {
  const when = useWhen();
  const navigate = useNavigate();
  const [note, setNote] = useState("");
  const [member, setMember] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const audit = useCaseAudit(value.id, showAudit);
  const client = useQueryClient();
  const open = value.status === "open";
  async function perform(action: () => Promise<unknown>, after?: () => void) {
    setMessage(null);
    try {
      await action();
      await client.invalidateQueries({ queryKey: caseKeys.all });
      after?.();
    } catch (problem) {
      setMessage(failureText(problem, "That could not be done."));
    }
  }
  const base = `/api/cases/${encodeURIComponent(value.id)}`;

  function openClip(item: CaseItem) {
    if (!item.camera_id || !item.starts_at) return;
    void api(`${base}/items/${item.id}/viewed`, { method: "POST" }).catch(() => undefined); // Recorded for the case; opening is never held up by it
    usePlaybackStore.getState().openInPlayback(item.camera_id, Date.parse(item.starts_at));
    navigate("/playback");
  }

  return (
    <section className="cases case-view" aria-labelledby="case-title">
      <header className="cases-head">
        <h1 id="case-title">{value.title}</h1>
        <span className="case-status" data-state={value.status}>{open ? "Open" : `Closed ${when(value.closed_at)}`}</span>
        <Link className="button" data-variant="ghost" to="/cases">All cases</Link>
      </header>
      <p className="muted">Owner {value.owner ?? "none"} · started {when(value.created_at)}{open ? ` · ${value.held_segments} recorded interval${value.held_segments === 1 ? "" : "s"} held` : ""}</p>
      {value.description ? <p className="case-description">{value.description}</p> : null}
      {message ? <p className="notice" data-tone="bad" role="alert">{message}</p> : null}

      <section aria-labelledby="case-items-title">
        <h2 id="case-items-title">Items</h2>
        {value.restricted_items ? <p className="muted">{value.restricted_items} item{value.restricted_items === 1 ? " is" : "s are"} on cameras you may not see and not shown.</p> : null}
        {!value.items.length ? <p className="muted">Nothing in this case yet. Add clips and marks from Playback.</p> : null}
        <ul className="status-list case-items" aria-label="Case items">
          {value.items.map(item => (
            <li key={item.id} data-kind={item.kind} data-restricted={item.restricted}>
              <span className="case-kind">{KIND_LABEL[item.kind]}</span>
              <span className="case-item-text">{itemSummary(item, when)}</span>
              {!item.restricted ? <span className="muted">{item.added_by ? `${item.added_by}, ` : ""}{when(item.added_at)}</span> : null}
              <span className="case-item-actions">
                {!item.restricted && (item.kind === "clip" || item.kind === "mark" || item.kind === "alarm") && item.camera_id
                  ? <button type="button" className="button" data-variant="ghost" onClick={() => openClip(item)}>Open in Playback</button> : null}
                {open && !item.restricted && item.kind !== "note"
                  ? <button type="button" className="button" data-variant="ghost" aria-label={`Remove ${(KIND_LABEL[item.kind] ?? item.kind).toLowerCase()} from the case`} onClick={() => void perform(() => api(`${base}/items/${item.id}`, { method: "DELETE" }))}>Remove</button> : null}
              </span>
            </li>
          ))}
        </ul>
        {open ? (
          <form className="case-note" aria-label="Add a note" onSubmit={event => { event.preventDefault(); void perform(() => addItem(value.id, { kind: "note", text: note }), () => setNote("")); }}>
            <label className="field"><span>Add a note</span><textarea value={note} rows={2} maxLength={5000} onChange={event => setNote(event.target.value)} /></label>
            <button type="submit" className="button" disabled={!note.trim()}>Add note</button>
            <span className="muted">Notes stay as written: add another to correct one.</span>
          </form>
        ) : null}
      </section>

      <section aria-labelledby="case-members-title">
        <h2 id="case-members-title">Members</h2>
        <ul className="status-list" aria-label="Case members">
          {value.members.map(item => (
            <li key={item.user_id}>
              <span>{item.username}{item.owner ? " (owner)" : ""}</span>
              {value.can_manage && !item.owner ? <button type="button" className="button" data-variant="ghost" aria-label={`Remove ${item.username} from the case`} onClick={() => void perform(() => api(`${base}/members/${item.user_id}`, { method: "DELETE" }))}>Remove</button> : null}
            </li>
          ))}
        </ul>
        {value.can_manage ? (
          <form className="case-member" aria-label="Add a member" onSubmit={event => { event.preventDefault(); void perform(() => api(`${base}/members`, { method: "POST", body: { username: member } }), () => setMember("")); }}>
            <label className="field"><span>Add a member by user name</span><input value={member} onChange={event => setMember(event.target.value)} /></label>
            <button type="submit" className="button" disabled={!member.trim()}>Add member</button>
          </form>
        ) : null}
        <p className="muted">Only members and administrators can open a case. Each person sees the clips on the cameras they may see.</p>
      </section>

      <section aria-labelledby="case-package-title">
        <h2 id="case-package-title">Evidence package</h2>
        <p className="muted">One file with every clip you may see, the notes and the audit, with a manifest it can be checked against.</p>
        <button type="button" className="button" disabled={!value.items.some(item => item.kind === "clip" && !item.restricted) || value.packages.some(item => item.status === "building")}
                onClick={() => void perform(() => api(`${base}/packages`, { method: "POST" }))}>Build evidence package</button>
        <ul className="status-list" aria-label="Evidence packages">
          {value.packages.map(item => <PackageRow key={item.id} caseId={value.id} item={item} when={when} />)}
        </ul>
      </section>

      <section aria-labelledby="case-audit-title">
        <h2 id="case-audit-title">Audit</h2>
        <button type="button" className="button" data-variant="ghost" aria-expanded={showAudit} onClick={() => setShowAudit(!showAudit)}>{showAudit ? "Hide the audit trail" : "Show the audit trail"}</button>
        {showAudit ? (
          <ul className="status-list" aria-label="Audit trail">
            {(audit.data ?? []).map((event, index) => <li key={index}><span>{event.actor ?? "someone"}: {event.action.replace(/_/g, " ")}</span><span className="muted">{when(event.at)}</span></li>)}
          </ul>
        ) : null}
      </section>

      {open && value.can_manage ? (
        <section aria-labelledby="case-close-title">
          <h2 id="case-close-title">Close the case</h2>
          <button type="button" className="button" data-variant="danger" onClick={() => setConfirming(true)}>Close case…</button>
        </section>
      ) : null}
      <Modal open={confirming} onOpenChange={setConfirming} title="Close this case?"
             description={`Closing releases the hold on ${value.held_segments} recorded interval${value.held_segments === 1 ? "" : "s"}. Footage nothing else holds becomes eligible for deletion under its retention policy.`}
             footer={<>
               <button type="button" className="button" onClick={() => setConfirming(false)}>Keep it open</button>
               <button type="button" className="button" data-variant="danger" onClick={() => { setConfirming(false); void perform(() => api(`${base}/close`, { method: "POST", body: { confirm: true } })); }}>Close case and release holds</button>
             </>}>
        <p>You can still open a closed case and build a package of what it holds, but its footage is no longer protected.</p>
      </Modal>
    </section>
  );
}

function PackageRow({ caseId, item, when }: { caseId: string; item: CasePackage; when: (iso: string | null | undefined) => string }) {
  const [result, setResult] = useState<PackageCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function verify(file: File | undefined) {
    if (!file) return;
    setChecking(true);
    setProblem(null);
    setResult(null);
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/packages/${encodeURIComponent(item.id)}/verify`, { method: "POST", credentials: "same-origin", body: file });
      const body = (await response.json()) as PackageCheck & { detail?: string };
      if (!response.ok) setProblem(body.detail ?? "The package could not be checked.");
      else setResult(body);
    } catch {
      setProblem("The NVR could not be reached.");
    } finally {
      setChecking(false);
    }
  }

  return (
    <li data-state={item.status}>
      <span>{when(item.created_at)}: {item.status === "building" ? "being built…" : item.status === "ready" ? `ready (${Math.round((item.size_bytes ?? 0) / 1024)} KB)` : `failed: ${item.error ?? "unknown reason"}`}{item.omitted_items ? ` · ${item.omitted_items} item${item.omitted_items === 1 ? "" : "s"} left out (cameras you may not see)` : ""}</span>
      {item.status === "ready" ? (
        <span className="case-item-actions">
          <a className="button" href={`/api/cases/${encodeURIComponent(caseId)}/packages/${encodeURIComponent(item.id)}/download`} download>Download</a>
          <label className="button">{checking ? "Checking…" : "Verify a file…"}<input type="file" accept=".zip" className="visually-hidden" onChange={event => void verify(event.target.files?.[0])} /></label>
        </span>
      ) : null}
      {problem ? <p className="notice" data-tone="bad" role="alert">{problem}</p> : null}
      {result ? (
        <p className="notice" role="status" data-tone={result.checksums_valid && result.clips_valid && result.complete ? "good" : "bad"}>
          {result.checksums_valid && result.clips_valid ? `The files match their manifest and ${result.clips.length === 1 ? "its clip package verifies" : `all ${result.clips.length} clip packages verify`}.` : (result.error ?? "This package does not verify.")}
          {result.checksums_valid && result.clips_valid && !result.complete ? ` Not every clip of the case is in it${result.omitted_items ? ` (${result.omitted_items} left out)` : ""}.` : ""}
        </p>
      ) : null}
    </li>
  );
}
