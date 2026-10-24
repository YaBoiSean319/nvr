import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "@/api/client";
import { keys, useCameras } from "@/api/queries";
import type { Camera } from "@/api/types";
import { formatRelative } from "@/lib/time";
import { failureText } from "../../tabDraft";
import { useCamerasStore } from "../store";
import { ConnectionFields, EMPTY_CONNECTION, sourceBody, type Connection } from "./ConnectionFields";

export interface Candidate {
  id: string;
  device_uuid: string;
  service_urls: string[];
  scopes: string[];
  capabilities: Record<string, string>;
  linked_camera_id: string | null;
  add_in_progress: boolean;
  last_seen_at: string;
}

interface ScanResult {
  scan_mode: "multicast" | "ip_range";
  targets_scanned: number;
  discovered_count: number;
  ignored_responses: number;
  partial_failures: string[];
  candidates: Candidate[];
}

export function candidateAddress(candidate: Candidate): string {
  for (const url of candidate.service_urls) {
    try {
      const host = new URL(url).hostname.replace(/^\[|\]$/g, "");
      if (host) return host;
    } catch {
      // not a URL
    }
  }
  return "";
}

/** Only a non-standard device service path needs to be kept; otherwise address and port build it. */
function candidateEndpoint(candidate: Candidate): string {
  for (const url of candidate.service_urls) {
    try {
      if (new URL(url).pathname !== "/onvif/device_service") return url;
    } catch {
      // not a URL
    }
  }
  return "";
}

function candidatePort(candidate: Candidate): string {
  for (const url of candidate.service_urls) {
    try {
      const parsed = new URL(url);
      return parsed.port || (parsed.protocol === "https:" ? "443" : "80");
    } catch {
      // not a URL
    }
  }
  return "80";
}

export function candidateLabel(candidate: Candidate): string {
  for (const scope of candidate.scopes) {
    const match = /(?:^|\/)name\/([^/?#]+)/i.exec(scope);
    if (match?.[1]) {
      try {
        return decodeURIComponent(match[1]).trim() || match[1];
      } catch {
        return match[1];
      }
    }
  }
  const address = candidateAddress(candidate);
  if (candidate.device_uuid.startsWith("ipscan:")) return address ? `Device at ${address}` : "Address-based device";
  return address ? `ONVIF device at ${address}` : "Unknown ONVIF device";
}

type ScanRequest = { timeout_seconds?: number; ip_range?: string };

/** Scan the camera network, review unadded devices and set one up at a time. */
export function Discover() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const cameras = useCameras();
  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [scan, setScan] = useState<{ busy: boolean; error?: string; last?: ScanResult; request?: ScanRequest }>({ busy: false });
  const [timeout, setTimeoutSeconds] = useState("5");
  const [range, setRange] = useState("");
  const [query, setQuery] = useState("");
  const [showAdded, setShowAdded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const generation = useRef(0);

  // Only the newest candidate list may replace the table.
  async function refresh() {
    const mine = ++generation.current;
    try {
      const result = await api<{ candidates: Candidate[] }>("/api/discovery/candidates");
      if (mine === generation.current) {
        setCandidates(result.candidates);
        setLoadError(null);
      }
    } catch (error) {
      if (mine === generation.current) setLoadError(failureText(error, "Discovered devices could not be loaded."));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function runScan(request: ScanRequest) {
    const mine = ++generation.current;
    setScan({ busy: true, request });
    try {
      const result = await api<ScanResult>("/api/discovery/scan", { method: "POST", body: request });
      if (mine !== generation.current) return;
      setCandidates(result.candidates);
      setScan({ busy: false, last: result, request });
    } catch (error) {
      if (mine !== generation.current) return;
      setScan(current => ({ ...current, busy: false, error: failureText(error, "the NVR could not be reached"), request }));
    }
  }

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (candidates ?? [])
      .filter(candidate => showAdded || !candidate.linked_camera_id)
      .filter(candidate => !needle || [candidateLabel(candidate), candidateAddress(candidate), candidate.device_uuid].join(" ").toLowerCase().includes(needle))
      .sort((a, b) => candidateLabel(a).localeCompare(candidateLabel(b), undefined, { numeric: true }) || a.id.localeCompare(b.id));
  }, [candidates, query, showAdded]);
  const selected = (candidates ?? []).find(candidate => candidate.id === selectedId) ?? null;
  const notAdded = (candidates ?? []).filter(candidate => !candidate.linked_camera_id).length;
  const byId = new Map((cameras.data ?? []).map(camera => [camera.id, camera]));

  return (
    <div className="add-layout">
      <section className="add-list" aria-label="Discovered devices">
        <div className="discovery-scan" role="group" aria-label="Discovery scan">
          <button type="button" className="button" data-variant="primary" disabled={scan.busy} onClick={() => void runScan({ timeout_seconds: Number(timeout) || 5 })}>Scan camera network</button>
          <label className="inline-field">Duration (s) <input type="number" min={0.5} max={15} step={0.5} value={timeout} onChange={event => setTimeoutSeconds(event.target.value)} /></label>
          <label className="inline-field">Private range <input className="mono" placeholder="192.168.10.0/24" value={range} onChange={event => setRange(event.target.value)} /></label>
          <button type="button" className="button" disabled={scan.busy || !range.trim()} onClick={() => void runScan({ ip_range: range.trim() })}>Scan range</button>
        </div>
        <div className="discovery-status" aria-live="polite">
          {scan.busy ? <span role="status">{scan.request?.ip_range ? `Scanning ${scan.request.ip_range}…` : "Scanning the camera network…"}</span> : null}
          {!scan.busy && scan.error ? (
            <span className="field-error" role="alert">
              Scan failed: {scan.error}
              <button type="button" className="button" onClick={() => void runScan(scan.request ?? { timeout_seconds: 5 })}>Retry scan</button>
            </span>
          ) : null}
          {!scan.busy && !scan.error && scan.last ? (
            <span role="status">
              Scan complete: {scan.last.discovered_count} device(s) found{scan.last.scan_mode === "ip_range" ? ` across ${scan.last.targets_scanned} address(es)` : ""}.
              {scan.last.partial_failures.length ? ` ${scan.last.partial_failures.join(" ")}` : ""}
            </span>
          ) : null}
        </div>
        <div className="inventory-tools">
          <input type="search" className="browser-search" placeholder="Search discovered devices" aria-label="Search discovered devices" value={query} onChange={event => setQuery(event.target.value)} />
          <label className="inventory-toggle"><input type="checkbox" checked={showAdded} onChange={event => setShowAdded(event.target.checked)} /> Show added devices</label>
        </div>
        <p className="inventory-meta">{showAdded ? `${visible.length} devices shown` : `${notAdded} not-added device${notAdded === 1 ? "" : "s"} shown`}</p>
        <div className="inventory-scroll">
          {loadError ? <p className="notice inventory-empty" data-tone="bad" role="alert">{loadError}</p> : null}
          {candidates && !visible.length ? <p className="muted inventory-empty">{candidates.length ? "No devices match." : "No devices discovered yet. Run a scan."}</p> : null}
          {visible.length ? (
            <table className="inventory-table" aria-label="Discovered cameras">
              <thead><tr><th scope="col">Device</th><th scope="col">Address</th><th scope="col">State</th><th scope="col">Last seen</th></tr></thead>
              <tbody>
                {visible.map(candidate => (
                  <tr key={candidate.id} aria-selected={candidate.id === selectedId} onClick={() => setSelectedId(candidate.id)}>
                    <td><button type="button" className="row-select" onClick={() => setSelectedId(candidate.id)}>{candidateLabel(candidate)}</button></td>
                    <td className="mono">{candidateAddress(candidate) || <span className="muted">Not reported</span>}</td>
                    <td>{candidate.linked_camera_id ? "Already added" : candidate.add_in_progress ? "Adding…" : "Not added"}</td>
                    <td>{formatRelative(candidate.last_seen_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
      </section>
      <section className="add-detail" aria-label="Device setup">
        {selected ? (
          <CandidateSetup key={selected.id} candidate={selected} cameras={cameras.data ?? []} existing={selected.linked_camera_id ? byId.get(selected.linked_camera_id) : undefined}
                          onChanged={async () => { await refresh(); await client.invalidateQueries({ queryKey: keys.cameras }); }}
                          onOpenExisting={id => { useCamerasStore.getState().select(id); navigate("/cameras"); }} />
        ) : <div className="editor-empty"><p><strong>Select a discovered device to set it up.</strong></p></div>}
      </section>
    </div>
  );
}

function CandidateSetup({ candidate, existing, cameras, onChanged, onOpenExisting }: {
  candidate: Candidate; existing: Camera | undefined; cameras: Camera[]; onChanged: () => Promise<void>; onOpenExisting: (id: string) => void;
}) {
  const [name, setName] = useState(candidateLabel(candidate));
  const [connection, setConnection] = useState<Connection>({ ...EMPTY_CONNECTION, address: candidateAddress(candidate), port: candidatePort(candidate), endpoint: candidateEndpoint(candidate) });
  const [linkTo, setLinkTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ tone: "good" | "bad"; text: string } | null>(null);

  if (candidate.linked_camera_id) {
    return (
      <div className="tab-form">
        <h2 className="setup-title">{candidateLabel(candidate)}</h2>
        {result ? <p className="notice" data-tone={result.tone} role={result.tone === "good" ? "status" : "alert"}>{result.text}</p> : null}
        <p className="notice">Already added as <strong>{existing?.name ?? "a camera"}</strong>. It cannot be added again.</p>
        <div className="field-actions">
          <button type="button" className="button" onClick={() => onOpenExisting(candidate.linked_camera_id!)}>Open existing camera</button>
          <button type="button" className="button" data-variant="primary" disabled>Test connection and add camera</button>
        </div>
      </div>
    );
  }

  async function add() {
    setBusy(true);
    setResult(null);
    try {
      const camera = await api<Camera>(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/add`, {
        method: "POST",
        body: { ...sourceBody(connection), name: name.trim(), motion_username: connection.username.trim() || null, motion_password: connection.password || null, recording_mode: "motion", recording_enabled: false },
      });
      setResult({ tone: "good", text: `${camera.name} was added. Recording stays off until you turn it on.` });
      await onChanged();
    } catch (error) {
      // The draft stays as entered so it can be corrected.
      setResult({ tone: "bad", text: `Not added: ${failureText(error)}.` });
    } finally {
      setBusy(false);
    }
  }

  async function link() {
    setBusy(true);
    setResult(null);
    try {
      await api(`/api/discovery/candidates/${encodeURIComponent(candidate.id)}/link`, { method: "POST", body: { camera_id: linkTo } });
      setResult({ tone: "good", text: "Linked. The camera ID and recording history were preserved." });
      await onChanged();
    } catch (error) {
      setResult({ tone: "bad", text: `Not linked: ${failureText(error)}.` });
    } finally {
      setBusy(false);
    }
  }

  const linkable = cameras.filter(camera => !camera.archived_at);
  return (
    <form className="tab-form" aria-label={`Set up ${candidateLabel(candidate)}`} noValidate onSubmit={event => { event.preventDefault(); void add(); }}>
      <h2 className="setup-title">{candidateLabel(candidate)}</h2>
      <p className="muted field-hint">Identity {candidate.device_uuid} · capabilities: {Object.entries(candidate.capabilities).map(([key, value]) => `${key.replace(/_/g, " ")} ${value}`).join(", ") || "not reported"}</p>
      <label className="field"><span>Camera name</span><input value={name} onChange={event => setName(event.target.value)} maxLength={120} /></label>
      <ConnectionFields value={connection} onChange={setConnection} allowUrl={false} />
      <p className="muted field-hint">New cameras use motion recording and start with recording turned off.</p>
      {result ? <p className="notice" data-tone={result.tone} role={result.tone === "good" ? "status" : "alert"}>{result.text}</p> : null}
      <div className="field-actions">
        <button type="submit" className="button" data-variant="primary" disabled={busy || !name.trim() || !connection.address.trim()}>{busy ? "Testing…" : "Test connection and add camera"}</button>
      </div>
      {linkable.length ? (
        <fieldset className="field-group">
          <legend>Or link to an existing camera</legend>
          <p className="muted field-hint">Use this when the device is already in the inventory; its camera ID and history stay the same.</p>
          <div className="field-actions">
            <select aria-label="Existing camera" value={linkTo} onChange={event => setLinkTo(event.target.value)}>
              <option value="">Choose a camera…</option>
              {linkable.map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}
            </select>
            <button type="button" className="button" disabled={busy || !linkTo} onClick={() => void link()}>Link to existing camera</button>
          </div>
        </fieldset>
      ) : null}
    </form>
  );
}
