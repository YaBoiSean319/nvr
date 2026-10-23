import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";
import { api } from "@/api/client";
import { keys } from "@/api/queries";
import type { Camera, StreamProbe } from "@/api/types";
import { failureText } from "../../tabDraft";
import { useCamerasStore } from "../store";
import { ConnectionFields, EMPTY_CONNECTION, sourceBody, type Connection } from "./ConnectionFields";

/** Add one camera by address or URL after a successful stream test on the server. */
export function ManualAdd() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [connection, setConnection] = useState<Connection>(EMPTY_CONNECTION);
  const [busy, setBusy] = useState<"test" | "add" | null>(null);
  const [result, setResult] = useState<{ tone: "good" | "bad"; text: string; added?: Camera } | null>(null);
  const ready = name.trim() && (connection.mode === "onvif" ? connection.address.trim() : connection.stream_url.trim());

  async function test() {
    setBusy("test");
    setResult(null);
    try {
      const probe = await api<StreamProbe>("/api/cameras/test", { method: "POST", body: sourceBody(connection) });
      setResult(probe.reachable
        ? { tone: "good", text: `Stream opened: ${probe.video?.codec?.toUpperCase() ?? "video"} ${probe.video?.width ?? "?"}×${probe.video?.height ?? "?"}${probe.audio ? " with audio" : ""}.` }
        : { tone: "bad", text: `Stream did not open: ${probe.reason ?? "no reason reported"}.` });
    } catch (error) {
      setResult({ tone: "bad", text: `Test failed: ${failureText(error)}.` });
    } finally {
      setBusy(null);
    }
  }

  async function add() {
    setBusy("add");
    setResult(null);
    try {
      const camera = await api<Camera>("/api/cameras", {
        method: "POST",
        body: { ...sourceBody(connection), name: name.trim(), recording_mode: "motion", recording_enabled: false },
      });
      await client.invalidateQueries({ queryKey: keys.cameras });
      setResult({ tone: "good", text: `${camera.name} was added. Recording stays off until you turn it on.`, added: camera });
      setName("");
      setConnection(EMPTY_CONNECTION);
    } catch (error) {
      // Keep everything entered so the problem can be corrected.
      setResult({ tone: "bad", text: `Not added: ${failureText(error)}.` });
    } finally {
      setBusy(null);
    }
  }

  return (
    <form className="tab-form add-manual" aria-label="Add a camera manually" noValidate onSubmit={event => { event.preventDefault(); void add(); }}>
      <label className="field"><span>Camera name</span><input value={name} onChange={event => setName(event.target.value)} maxLength={120} /></label>
      <ConnectionFields value={connection} onChange={setConnection} />
      <p className="muted field-hint">The NVR tests the stream before adding it. New cameras use motion recording and start with recording turned off.</p>
      {result ? (
        <p className="notice" data-tone={result.tone} role={result.tone === "good" ? "status" : "alert"}>
          {result.text}
          {result.added ? <button type="button" className="button" onClick={() => { useCamerasStore.getState().select(result.added!.id); navigate("/cameras"); }}>Open {result.added.name}</button> : null}
        </p>
      ) : null}
      <div className="field-actions">
        <button type="button" className="button" disabled={busy !== null || !ready} onClick={() => void test()}>{busy === "test" ? "Testing…" : "Test stream"}</button>
        <button type="submit" className="button" data-variant="primary" disabled={busy !== null || !ready}>{busy === "add" ? "Adding…" : "Add camera"}</button>
      </div>
    </form>
  );
}
