import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/api/client";
import type { Camera } from "@/api/types";
import { failureText } from "@/features/admin/tabDraft";
import { FACES_KEY, useFaceEntries, type FaceEntry } from "@/features/settings/FacesSettings";
import { playbackClock } from "./clock";
import { usePlaybackStore } from "./store";

interface Box { x: number; y: number; width: number; height: number }
interface Found { camera_id: string; at: string; picture: string | null; faces: { box: Box; score: number; pixels: [number, number]; usable: boolean; reason: string | null }[] }

/** The order to draw the faces in: biggest box first, so the smallest is on top. */
const drawOrder = (faces: Found["faces"]) => faces.map((_, index) => index).sort((a, b) => faces[b]!.box.width * faces[b]!.box.height - faces[a]!.box.width * faces[a]!.box.height || a - b);

/** Choose a face in the picture at the playhead and put that person on the face watch list (or add another picture of someone already on it). Administrators only. */
export function FaceSearch({ shownIds, cameras }: { shownIds: string[]; cameras: Map<string, Camera> }) {
  const client = useQueryClient();
  const active = usePlaybackStore(store => store.activeId);
  const [cameraId, setCameraId] = useState<string>(() => (active && shownIds.includes(active) ? active : shownIds[0] ?? ""));
  const chosenCamera = shownIds.includes(cameraId) ? cameraId : shownIds[0] ?? "";
  const [found, setFound] = useState<Found | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [entryId, setEntryId] = useState("");
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [expires, setExpires] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const entries = useFaceEntries();
  const list: FaceEntry[] = entries.data?.entries ?? [];

  const look = useMutation({
    mutationFn: () => api<Found>("/api/faces/find", { method: "POST", body: { camera_id: chosenCamera, at: new Date(playbackClock.time()).toISOString() } }),
    onMutate: () => { setFound(null); setPicked(null); setMessage(null); },
    onSuccess: setFound,
  });
  const add = useMutation({
    mutationFn: () => {
      const face = found!.faces[picked!]!;
      const body: Record<string, unknown> = { camera_id: found!.camera_id, at: found!.at, box: face.box };
      if (mode === "existing") body.entry_id = entryId;
      else {
        body.name = name.trim();
        body.reason = reason.trim();
        if (expires) body.expires_at = new Date(`${expires}T23:59:59`).toISOString();
      }
      return api<FaceEntry>("/api/faces/watch", { method: "POST", body });
    },
    onSuccess: async entry => {
      setMessage(`${entry.name} is on the face watch list with ${entry.embeddings} ${entry.embeddings === 1 ? "picture" : "pictures"}. Turn face watch on for cameras, and make an alert rule, under Settings, Face watch.`);
      setFound(null); setPicked(null); setName(""); setReason(""); setExpires("");
      await client.invalidateQueries({ queryKey: FACES_KEY });
    },
  });

  const submit = (event: FormEvent) => { event.preventDefault(); if (found && picked !== null) add.mutate(); };
  const ready = picked !== null && (mode === "existing" ? entryId !== "" : name.trim() !== "" && reason.trim() !== "");
  return (
    <aside className="export-drawer" id="face-search" aria-label="Face watch">
      <header className="export-head">
        <h2>Find a face</h2>
        <button type="button" className="button icon-button" data-variant="ghost" aria-label="Close face watch" onClick={() => usePlaybackStore.getState().setFaceSearchOpen(false)}>✕</button>
      </header>
      <div className="export-body attr-body">
        <p className="muted">Pause on a moment where the person's face is clear, look at the picture there, and choose their face. They are then looked for on the cameras an administrator switches on under Settings, Face watch. Only matches are kept.</p>
        {shownIds.length === 0 ? <p className="muted">No cameras are shown in Playback.</p> : (
          <div className="attr-form">
            <label className="attr-field"><span>Camera</span>
              <select value={chosenCamera} onChange={event => setCameraId(event.target.value)}>
                {shownIds.map(id => <option key={id} value={id}>{cameras.get(id)?.name ?? id}</option>)}
              </select>
            </label>
            <div className="attr-actions">
              <button type="button" className="button" data-variant="primary" disabled={look.isPending || !chosenCamera} onClick={() => look.mutate()}>{look.isPending ? "Looking…" : "Look at the playhead"}</button>
            </div>
          </div>
        )}
        {look.error ? <p className="notice" data-tone="bad" role="alert">{failureText(look.error, "The picture could not be read.")}</p> : null}
        {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
        {found ? (
          found.faces.length === 0 ? <p className="muted" role="status">No face was found in that picture. Try a moment where the face is larger and facing the camera.</p> : !found.picture ? (
            // Nobody puts a face on the list without seeing it: faces were found, but there is no picture to choose on.
            <p className="notice" data-tone="bad" role="alert">The picture could not be shown, so no face can be chosen from it. Look at the playhead again to try once more.</p>
          ) : (
            <>
              <p className="muted" role="status">{found.faces.length} {found.faces.length === 1 ? "face" : "faces"} found. Choose one.</p>
              <div className="face-picture" style={{ position: "relative" }}>
                <img src={`data:image/jpeg;base64,${found.picture}`} alt="The picture at the playhead" style={{ width: "100%", display: "block" }} />
                {/* Largest first, so a smaller box inside a larger one is drawn over it and is the one under the pointer, whatever order the faces were found in. */}
                {drawOrder(found.faces).map(index => {
                  const face = found.faces[index]!;
                  const chosen = picked === index;
                  return (
                    <button key={index} type="button" aria-label={`Face ${index + 1}${face.usable ? "" : `, not usable: ${face.reason ?? ""}`}`} aria-pressed={chosen} disabled={!face.usable}
                            title={face.usable ? `Face ${index + 1}` : face.reason ?? ""} onClick={() => setPicked(index)}
                            style={{ position: "absolute", left: `${face.box.x * 100}%`, top: `${face.box.y * 100}%`, width: `${face.box.width * 100}%`, height: `${face.box.height * 100}%`,
                                     border: `${chosen ? 4 : 2}px ${face.usable ? "solid" : "dashed"} ${chosen ? "#2ecc71" : "#f1c40f"}`, boxShadow: chosen ? "0 0 0 2px rgba(0,0,0,0.6)" : undefined,
                                     background: chosen ? "rgba(46, 204, 113, 0.18)" : "transparent", padding: 0, cursor: face.usable ? "pointer" : "not-allowed" }} />
                  );
                })}
              </div>
              {picked !== null ? <p className="muted" role="status">Face {picked + 1} is chosen.</p> : null}
              {picked !== null ? (
                <form className="attr-form" onSubmit={submit} aria-label="Add this face">
                  <label className="check"><input type="radio" name="face-mode" checked={mode === "new"} onChange={() => setMode("new")} /> A new person</label>
                  <label className="check"><input type="radio" name="face-mode" checked={mode === "existing"} disabled={list.length === 0} onChange={() => setMode("existing")} /> Another picture of someone on the list</label>
                  {mode === "existing" ? (
                    <label className="attr-field"><span>Who</span>
                      <select value={entryId} onChange={event => setEntryId(event.target.value)}>
                        <option value="">Choose…</option>
                        {list.map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
                      </select>
                    </label>
                  ) : (
                    <>
                      <label className="attr-field"><span>Name or label</span><input value={name} maxLength={120} onChange={event => setName(event.target.value)} /></label>
                      <label className="attr-field"><span>Why they are on the list</span><textarea value={reason} maxLength={500} rows={3} onChange={event => setReason(event.target.value)} /></label>
                      <label className="attr-field"><span>Remove from the list after (optional)</span><input type="date" value={expires} onChange={event => setExpires(event.target.value)} /></label>
                    </>
                  )}
                  {add.error ? <p className="notice" data-tone="bad" role="alert">{failureText(add.error, "The face could not be added.")}</p> : null}
                  <div className="attr-actions"><button type="submit" className="button" data-variant="primary" disabled={!ready || add.isPending}>{add.isPending ? "Adding…" : "Add to the face watch list"}</button></div>
                </form>
              ) : null}
            </>
          )
        ) : null}
      </div>
    </aside>
  );
}
