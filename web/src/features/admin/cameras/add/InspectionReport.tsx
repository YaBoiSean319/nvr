import type { InspectReport } from "@/api/types";
import { CAPABILITY_LABEL, STATE_TEXT } from "./inspect";

const MARK: Record<string, string> = { ok: "✓", failed: "✕", skipped: "–", pending: "·" };

/** The steps of an inspection with each outcome, the capabilities judged separately, and the redacted diagnostics on request. */
export function InspectionReport({ report }: { report: InspectReport }) {
  const device = [report.device.manufacturer, report.device.model].filter(Boolean).join(" ");
  const record = report.profiles.find(profile => profile.token === report.selection?.record_token);
  const live = report.profiles.find(profile => profile.token === report.selection?.live_token);
  const size = (profile: typeof record) => (profile ? `${profile.name || profile.token} (${profile.encoding.toUpperCase()}, ${profile.width}×${profile.height})` : null);
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "camera-diagnostics.json";
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <section className="inspection" aria-label="Camera check">
      <ol className="inspection-steps" aria-label="Steps">
        {report.steps.map(step => (
          <li key={step.id} data-state={step.state}>
            <span className="step-mark" aria-hidden="true">{MARK[step.state] ?? "·"}</span>
            <span className="step-label">{step.label}</span>
            <span className="step-state">{STATE_TEXT[step.state] ?? step.state}</span>
            {step.detail ? <span className="step-detail muted">{step.detail}</span> : null}
          </li>
        ))}
      </ol>
      {device ? <p><strong>{device}</strong>{report.device.firmware ? <span className="muted"> · firmware {report.device.firmware}</span> : null}</p> : null}
      {record ? (
        <p className="muted">Recording: {size(record)}.{live ? ` Live view: ${size(live)}.` : " Live view uses the main stream."}</p>
      ) : null}
      {report.selection?.warnings.map(warning => <p key={warning} className="field-error" role="status">{warning}</p>)}
      <table className="inventory-table inspection-capabilities" aria-label="What works">
        <thead><tr><th scope="col">Capability</th><th scope="col">State</th><th scope="col">Detail</th></tr></thead>
        <tbody>
          {Object.entries(report.capabilities).map(([name, capability]) => (
            <tr key={name} data-state={capability.state}>
              <th scope="row">{CAPABILITY_LABEL[name] ?? name}</th>
              <td>{STATE_TEXT[capability.state] ?? capability.state}</td>
              <td className="muted">{capability.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <details className="inspection-diagnostics">
        <summary>Diagnostics</summary>
        <p className="muted">Each request the NVR made to the camera. No passwords, and no addresses that contain a login, are included.</p>
        {report.diagnostics.length ? (
          <table className="inventory-table" aria-label="Requests made">
            <thead><tr><th scope="col">Operation</th><th scope="col">Service</th><th scope="col">Sign-in</th><th scope="col">Result</th><th scope="col">ms</th></tr></thead>
            <tbody>
              {report.diagnostics.map((entry, index) => (
                <tr key={index}>
                  <td className="mono">{String(entry.operation ?? "")}</td><td>{String(entry.service ?? "")}</td>
                  <td>{String(entry.auth ?? "")}</td><td>{String(entry.outcome ?? "")}{entry.status ? ` (${entry.status})` : ""}</td><td>{String(entry.ms ?? "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">No requests were made.</p>}
        <button type="button" className="button" onClick={download}>Download diagnostics</button>
      </details>
    </section>
  );
}
