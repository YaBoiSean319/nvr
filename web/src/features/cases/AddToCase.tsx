import { useState } from "react";
import { Link } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useCameras, useDisplaySettings } from "@/api/queries";
import { Modal } from "@/components/Modal";
import { failureText } from "@/features/admin/tabDraft";
import { playbackClock } from "@/features/playback/clock";
import { usePlaybackStore } from "@/features/playback/store";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { addItem, caseKeys, createCase, useCases } from "./api";

const CLIP_HALF_MS = 30_000;

/** From Playback: put the active camera's marked stretch (or the minute around the playhead) in a case, or mark the playhead; and reach the Cases list. */
export function AddToCase({ ready }: { ready: boolean }) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState("");
  const [title, setTitle] = useState("");
  const [label, setLabel] = useState("");
  const [result, setResult] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const cases = useCases();
  const client = useQueryClient();
  const cameras = useCameras();
  const display = useDisplaySettings();
  const activeId = usePlaybackStore(store => store.activeId);
  const cameraIds = usePlaybackStore(store => store.cameraIds);
  const marks = usePlaybackStore(store => store.marks);
  const camera = (cameras.data ?? []).find(item => item.id === (activeId ?? cameraIds[0]));
  const openCases = (cases.data ?? []).filter(item => item.status === "open");

  function range() {
    const now = playbackClock.time();
    // Footage already recorded: a minute around a playhead near the live edge stops at the present.
    const end = Math.min(marks.out ?? now + CLIP_HALF_MS, Date.now());
    const start = Math.min(marks.in ?? now - CLIP_HALF_MS, end - 1000);
    return { start, end };
  }
  const when = (moment: number) => (display.data ? formatSiteDateTime(moment, display.data) : new Date(moment).toISOString());

  async function add(kind: "clip" | "mark") {
    if (!camera) return;
    setBusy(true);
    setResult(null);
    try {
      let caseId = target;
      if (!caseId || caseId === "new") {
        const created = await createCase(title.trim() || `${camera.name} ${when(Date.now())}`, "");
        caseId = created.id;
        await client.invalidateQueries({ queryKey: caseKeys.all });
        setTarget(caseId);
      }
      if (kind === "clip") {
        const { start, end } = range();
        const added = await addItem(caseId, { kind: "clip", camera_id: camera.id, start: new Date(start).toISOString(), end: new Date(end).toISOString(), text: label });
        setResult({ tone: "good", text: `Clip added. ${added.case.held_segments} recorded interval${added.case.held_segments === 1 ? " is" : "s are"} now held.` });
      } else {
        await addItem(caseId, { kind: "mark", camera_id: camera.id, at: new Date(playbackClock.time()).toISOString(), text: label || "Marked in Playback" });
        setResult({ tone: "good", text: "Mark added." });
      }
      await client.invalidateQueries({ queryKey: caseKeys.all });
    } catch (problem) {
      setResult({ tone: "bad", text: failureText(problem, "That could not be added to the case.") });
    } finally {
      setBusy(false);
    }
  }

  const { start, end } = range();
  const chosen = target || (openCases[0]?.id ?? "new");
  return (
    <span className="case-controls" role="group" aria-label="Cases">
      <button type="button" className="button" disabled={!ready || !camera} onClick={() => { setOpen(true); setResult(null); }}>Add to case…</button>
      <Link className="button" data-variant="ghost" to="/cases">Cases</Link>
      <Modal open={open} onOpenChange={setOpen} title="Add to a case" description={camera ? `${camera.name}` : "Choose a camera in Playback first"}
             footer={<>
               <button type="button" className="button" onClick={() => setOpen(false)}>Done</button>
               <button type="button" className="button" disabled={busy || !camera} onClick={() => void add("mark")}>Mark the playhead</button>
               <button type="button" className="button" data-variant="primary" disabled={busy || !camera} onClick={() => void add("clip")}>Add clip</button>
             </>}>
        <label className="field"><span>Case</span>
          <select value={chosen} onChange={event => setTarget(event.target.value)}>
            {openCases.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
            <option value="new">A new case…</option>
          </select>
        </label>
        {chosen === "new" ? <label className="field"><span>New case title</span><input value={title} maxLength={200} onChange={event => setTitle(event.target.value)} /></label> : null}
        <label className="field"><span>Label (optional)</span><input value={label} maxLength={200} onChange={event => setLabel(event.target.value)} /></label>
        <p className="muted">Clip: {camera?.name ?? "no camera"} from {when(start)} to {when(end)}{marks.in === null && marks.out === null ? " (the minute around the playhead; mark in and out to choose)" : ""}. Its footage is held while the case is open.</p>
        {result ? <p className="notice" role="status" data-tone={result.tone}>{result.text}</p> : null}
      </Modal>
    </span>
  );
}
