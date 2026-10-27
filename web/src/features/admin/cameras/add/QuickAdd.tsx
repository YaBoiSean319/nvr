import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { keys } from "@/api/queries";
import type { Camera, CameraBrand, InspectReport } from "@/api/types";
import { BRAND_LABEL } from "../inventory";
import { useCamerasStore } from "../store";
import { InspectionReport } from "./InspectionReport";
import { attentionText, EMPTY_QUICK, failureText, inspectCamera, saveChecked, suggestedName, type QuickConnection } from "./inspect";

/** Add one camera from only its address, username and password: the NVR works out the rest and shows what it found. */
export function QuickAdd() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [connection, setConnection] = useState<QuickConnection>(EMPTY_QUICK);
  const [name, setName] = useState("");
  const [report, setReport] = useState<InspectReport | null>(null);
  const [checked, setChecked] = useState<QuickConnection | null>(null);
  const [busy, setBusy] = useState<"check" | "add" | null>(null);
  const [message, setMessage] = useState<{ tone: "good" | "bad" | "warn"; text: string; added?: Camera } | null>(null);
  const set = (change: Partial<QuickConnection>) => setConnection({ ...connection, ...change });
  const stale = report !== null && checked !== null && JSON.stringify(checked) !== JSON.stringify(connection);

  async function check() {
    setBusy("check");
    setMessage(null);
    setReport(null);
    try {
      const result = await inspectCamera(connection);
      setReport(result);
      setChecked(connection);
      setName(current => current || suggestedName(result, connection.host));
    } catch (error) {
      setMessage({ tone: "bad", text: `The camera could not be checked: ${failureText(error)}.` });
    } finally {
      setBusy(null);
    }
  }

  async function add() {
    if (!report || !checked) return;
    setBusy("add");
    setMessage(null);
    try {
      const camera = await saveChecked(checked, report, name);
      await client.invalidateQueries({ queryKey: keys.cameras });
      setMessage({
        tone: report.ok ? "good" : "warn",
        text: `${camera.name} was added. Recording stays off until you turn it on.${report.ok ? "" : ` It needs attention (${attentionText(report)}); the NVR keeps checking.`}`,
        added: camera,
      });
      setConnection(EMPTY_QUICK);
      setChecked(null);
      setReport(null);
      setName("");
    } catch (error) {
      // Keep everything entered so the problem can be corrected.
      setMessage({ tone: "bad", text: `Not added: ${failureText(error)}.` });
    } finally {
      setBusy(null);
    }
  }

  const ready = connection.host.trim() && Boolean(connection.username) === Boolean(connection.password);
  return (
    <form className="tab-form add-quick" aria-label="Add a camera by address" noValidate onSubmit={event => { event.preventDefault(); void check(); }}>
      <p className="muted field-hint">Enter the camera's address and login. The NVR identifies the camera, signs in, finds its streams and checks the video.</p>
      <label className="field"><span>Camera address</span>
        <input className="mono" value={connection.host} placeholder="IP address or host name" autoComplete="off" onChange={event => set({ host: event.target.value })} />
      </label>
      <div className="field-row">
        <label className="field"><span>Camera username</span><input value={connection.username} autoComplete="off" onChange={event => set({ username: event.target.value })} /></label>
        <label className="field"><span>Camera password</span><input type="password" value={connection.password} autoComplete="new-password" onChange={event => set({ password: event.target.value })} /></label>
      </div>
      <details className="quick-advanced">
        <summary>Advanced</summary>
        <label className="field"><span>ONVIF device service address (optional)</span>
          <input className="mono" value={connection.endpoint} placeholder="Found automatically" autoComplete="off" onChange={event => set({ endpoint: event.target.value })} />
        </label>
        <label className="field"><span>Brand</span>
          <select value={connection.brand} onChange={event => set({ brand: event.target.value as CameraBrand })}>
            {(Object.keys(BRAND_LABEL) as CameraBrand[]).map(brand => <option key={brand} value={brand}>{brand === "auto" ? "Detect automatically" : BRAND_LABEL[brand]}</option>)}
          </select>
        </label>
      </details>
      <div className="field-actions">
        <button type="submit" className="button" data-variant={report ? undefined : "primary"} disabled={busy !== null || !ready}>{busy === "check" ? "Checking the camera…" : report ? "Check again" : "Check camera"}</button>
      </div>
      {busy === "check" ? <p className="muted" role="status">Connecting, signing in, finding the streams and checking the video. This can take up to a minute.</p> : null}
      {report ? <InspectionReport report={report} /> : null}
      {report ? (
        <>
          <label className="field"><span>Camera name</span><input value={name} maxLength={120} onChange={event => setName(event.target.value)} /></label>
          {stale ? <p className="field-error" role="alert">The address or login changed since the check. Check the camera again before adding it.</p> : null}
          {!report.ok ? <p className="muted field-hint">The camera can still be added. Recording stays off, and the NVR keeps checking what is not working.</p> : null}
          <div className="field-actions">
            <button type="button" className="button" data-variant="primary" disabled={busy !== null || stale || !name.trim()} onClick={() => void add()}>{busy === "add" ? "Adding…" : report.ok ? "Add camera" : "Add camera anyway"}</button>
          </div>
        </>
      ) : null}
      {message ? (
        <p className="notice" data-tone={message.tone} role={message.tone === "bad" ? "alert" : "status"}>
          {message.text}
          {message.added ? <button type="button" className="button" onClick={() => { useCamerasStore.getState().select(message.added!.id); navigate("/cameras"); }}>Open {message.added.name}</button> : null}
        </p>
      ) : null}
    </form>
  );
}
