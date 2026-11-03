import { useState } from "react";
import { NavLink } from "react-router";
import type { Camera } from "@/api/types";
import { failureText } from "@/features/admin/tabDraft";
import { useEndWindow, useMaintenance, useOpenWindow } from "./api";

/** On a camera: start an hour of maintenance with one press, or end the one that is running. */
export function QuickMaintenance({ camera }: { camera: Camera }) {
  const list = useMaintenance();
  const open = useOpenWindow();
  const end = useEndWindow();
  const [error, setError] = useState<string | null>(null);
  const covered = list.data?.covered_camera_ids.includes(camera.id) ?? false;
  const own = list.data?.windows.find(item => item.state === "active" && item.scope === "cameras" && item.camera_ids.length === 1 && item.camera_ids[0] === camera.id);
  const fail = (problem: unknown) => setError(failureText(problem, "That did not work."));
  if (!list.data) return null;
  return (
    <>
      {covered ? (
        <>
          <NavLink to="/cameras/maintenance" className="tag" data-tone="warn">In maintenance</NavLink>
          {own ? <button type="button" className="button" disabled={end.isPending} onClick={() => end.mutate(own.id, { onError: fail })}>End maintenance</button> : null}
        </>
      ) : (
        <button type="button" className="button" disabled={open.isPending}
                onClick={() => { setError(null); open.mutate({ name: `Work on ${camera.name}`, reason: "", scope: "cameras", camera_ids: [camera.id], group_ids: [], duration_minutes: 60 }, { onError: fail }); }}>
          Start maintenance (1 hour)
        </button>
      )}
      {error ? <span className="muted" role="alert">{error}</span> : null}
    </>
  );
}
