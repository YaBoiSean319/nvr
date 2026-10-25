import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "@/api/client";
import { useCamerasStore } from "@/features/admin/cameras/store";
import { failureText } from "@/features/admin/tabDraft";
import { formatRelative } from "@/lib/time";

interface Finding {
  camera_id: string; camera_name: string; check: string; severity: "high" | "medium"; title: string; detail: string; fix: string;
  first_seen_at: string; acknowledged_at: string | null; acknowledged_by: string | null; reason: string | null;
}
interface Report {
  last_run_at: string | null; last_run_kind: "manual" | "scheduled" | null; next_run_at: string | null; camera_networks: string[]; network_check: boolean; probes_enabled: boolean;
  probe_gap_seconds: number; findings: Finding[]; acknowledged: Finding[]; counts: { high: number; medium: number; acknowledged: number };
}

const SEVERITY = { high: "High", medium: "Medium" } as const;
const key = (finding: Finding) => `${finding.camera_id}/${finding.check}`;

/** How exposed the cameras are: default passwords, unencrypted streams, and cameras off the camera network. */
export function HardeningSettings() {
  const client = useQueryClient();
  const report = useQuery({ queryKey: ["hardening"], queryFn: ({ signal }) => api<Report>("/api/hardening", { signal }) });
  const run = useMutation({ mutationFn: () => api<Report>("/api/hardening/run", { method: "POST" }), onSuccess: result => client.setQueryData(["hardening"], result) });
  const data = report.data;
  return (
    <>
      <section className="field-group settings-block" aria-label="Hardening checks">
        <h2 className="field-heading">Checks</h2>
        <p className="muted field-hint">
          Each camera is checked for a well-known default password, plain RTSP where the camera offers RTSPS, ONVIF over HTTP where it offers HTTPS, and an address outside the camera network.
          The password check compares the login the NVR already holds with a bundled list; nothing is tried against the camera, and the login is never logged or stored.
          Checks run every week and on demand, and ask a camera whether it offers a secure connection at most once an hour.
        </p>
        {report.isPending ? <p className="muted">Loading…</p> : report.error ? <p className="notice" data-tone="bad" role="alert">{failureText(report.error)}</p> : data ? (
          <p className="notice" role="status" data-tone={data.last_run_at ? (data.counts.high ? "bad" : data.counts.medium ? "warn" : "good") : undefined}>
            {data.last_run_at
              ? `${data.last_run_kind === "scheduled" ? "Weekly run" : "Run by hand"} ${formatRelative(data.last_run_at)}: ${data.counts.high + data.counts.medium ? `${data.counts.high} high and ${data.counts.medium} medium` : "no findings"}${data.counts.acknowledged ? `, ${data.counts.acknowledged} acknowledged` : ""}.`
              : `The checks have not run yet${data.next_run_at ? `; the first weekly run is ${formatRelative(data.next_run_at)}` : ""}. Run them now to see where the cameras stand.`}
          </p>
        ) : null}
        {run.error ? <p className="notice" data-tone="bad" role="alert">The checks could not run: {failureText(run.error)}.</p> : null}
        <div className="field-actions">
          <button type="button" className="button" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Checking…" : "Run checks now"}</button>
        </div>
      </section>
      {data ? <Networks report={data} /> : null}
      {data ? <Findings report={data} /> : null}
      {data && data.acknowledged.length ? <Acknowledged report={data} /> : null}
    </>
  );
}

function Networks({ report }: { report: Report }) {
  const client = useQueryClient();
  const saved = report.camera_networks.join("\n");
  const [text, setText] = useState(saved);
  const [probes, setProbes] = useState(report.probes_enabled);
  useEffect(() => setText(saved), [saved]);
  useEffect(() => setProbes(report.probes_enabled), [report.probes_enabled]);
  const save = useMutation({
    mutationFn: () => api<Report>("/api/hardening/settings", { method: "PUT", body: { camera_networks: text.split(/[\s,]+/).filter(Boolean), probes_enabled: probes } }),
    onSuccess: result => client.setQueryData(["hardening"], result),
  });
  return (
    <section className="field-group settings-block" aria-label="Camera networks and probes">
      <h2 className="field-heading">Camera networks and probes</h2>
      <p className="muted field-hint">
        The networks the cameras belong on, one per line, such as 192.168.50.0/24. A camera configured with an address outside them is reported.
        {report.network_check ? "" : " None are set, so this check is not being made."}
      </p>
      <form noValidate onSubmit={event => { event.preventDefault(); save.mutate(); }}>
        <label className="field"><span>Camera networks (one per line)</span>
          <textarea rows={3} value={text} onChange={event => setText(event.target.value)} spellCheck={false} placeholder="192.168.50.0/24" />
        </label>
        <label className="check"><input type="checkbox" checked={probes} onChange={event => setProbes(event.target.checked)} /> Ask cameras whether they offer RTSPS and HTTPS</label>
        <p className="muted field-hint">A short secure-connection handshake to a camera's usual RTSPS and HTTPS ports, at most once an hour per camera. Turn it off and the two protocol checks are not made.</p>
        {save.error ? <p className="notice" data-tone="bad" role="alert">{failureText(save.error)}.</p> : null}
        <div className="field-actions">
          <button type="submit" className="button" disabled={save.isPending || (text.split(/[\s,]+/).filter(Boolean).join("\n") === saved && probes === report.probes_enabled)}>{save.isPending ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </section>
  );
}

function Findings({ report }: { report: Report }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const acknowledge = useMutation({
    mutationFn: (finding: Finding) => api<Report>(`/api/hardening/findings/${finding.camera_id}/${finding.check}/acknowledgement`, { method: "PUT", body: { reason: reason.trim() } }),
    onSuccess: result => { client.setQueryData(["hardening"], result); setOpen(null); setReason(""); },
  });
  return (
    <section className="field-group settings-block" aria-label="Findings">
      <h2 className="field-heading">Findings</h2>
      {report.findings.length ? (
        <div className="inventory-scroll">
          <table className="inventory-table" aria-label="Hardening findings">
            <thead><tr><th scope="col">Severity</th><th scope="col">Camera</th><th scope="col">Finding</th><th scope="col">How to fix it</th><th scope="col">Action</th></tr></thead>
            <tbody>
              {report.findings.map(finding => (
                <tr key={key(finding)} data-severity={finding.severity}>
                  <td>{SEVERITY[finding.severity]}</td>
                  <td>
                    <button type="button" className="row-select" onClick={() => { useCamerasStore.getState().select(finding.camera_id); navigate("/cameras"); }}>{finding.camera_name}</button>
                  </td>
                  <td><strong>{finding.title}</strong><br />{finding.detail}</td>
                  <td>{finding.fix}</td>
                  <td>
                    {open === key(finding) ? (
                      <form noValidate onSubmit={event => { event.preventDefault(); acknowledge.mutate(finding); }}>
                        <label className="field"><span>Why is this accepted? ({finding.camera_name})</span>
                          <input value={reason} maxLength={300} onChange={event => setReason(event.target.value)} />
                        </label>
                        {acknowledge.error ? <p className="field-error" role="alert">{failureText(acknowledge.error)}.</p> : null}
                        <div className="field-actions">
                          <button type="submit" className="button" disabled={!reason.trim() || acknowledge.isPending}>Acknowledge</button>
                          <button type="button" className="button" onClick={() => { setOpen(null); setReason(""); }}>Cancel</button>
                        </div>
                      </form>
                    ) : (
                      <button type="button" className="button" aria-label={`Acknowledge ${finding.title} on ${finding.camera_name}`} onClick={() => { setOpen(key(finding)); setReason(""); }}>Acknowledge…</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">{report.last_run_at ? "No findings: every camera passed the checks that apply." : "Nothing to show until the checks have run."}</p>}
      <p className="muted field-hint">An acknowledged finding stays hidden until its condition changes, and returns if the weakness is fixed and later comes back.</p>
    </section>
  );
}

function Acknowledged({ report }: { report: Report }) {
  const client = useQueryClient();
  const restore = useMutation({
    mutationFn: (finding: Finding) => api<Report>(`/api/hardening/findings/${finding.camera_id}/${finding.check}/acknowledgement`, { method: "DELETE" }),
    onSuccess: result => client.setQueryData(["hardening"], result),
  });
  return (
    <section className="field-group settings-block" aria-label="Acknowledged findings">
      <h2 className="field-heading">Acknowledged</h2>
      {restore.error ? <p className="notice" data-tone="bad" role="alert">{failureText(restore.error)}.</p> : null}
      <ul className="plain-list" aria-label="Acknowledged findings list">
        {report.acknowledged.map(finding => (
          <li key={key(finding)}>
            <strong>{finding.camera_name}</strong>: {finding.title}. Accepted by {finding.acknowledged_by ?? "a removed user"} {finding.acknowledged_at ? formatRelative(finding.acknowledged_at) : ""}: {finding.reason}
            {" "}<button type="button" className="button" disabled={restore.isPending} aria-label={`Show ${finding.title} on ${finding.camera_name} again`} onClick={() => restore.mutate(finding)}>Show again</button>
          </li>
        ))}
      </ul>
    </section>
  );
}
