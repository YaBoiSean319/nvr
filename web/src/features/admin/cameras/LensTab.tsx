import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/api/client";
import type { Camera } from "@/api/types";
import type { Mount } from "@/features/dewarp/geometry";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";

interface LensSettings { kind: "normal" | "fisheye"; mount: Mount; center_x: number; center_y: number; radius: number; fov: number }

const NORMAL: LensSettings = { kind: "normal", mount: "ceiling", center_x: 0.5, center_y: 0.5, radius: 0.5, fov: 180 };
const MOUNT_LABEL: Record<Mount, string> = { ceiling: "Ceiling (looking down)", wall: "Wall (looking out)", floor: "Floor or table (looking up)" };
const percent = (value: number) => Math.round(value * 1000) / 10;

/** Whether the camera is a 360-degree fisheye camera, and where its lens circle is in the picture, so Live and exports can dewarp it. Recordings stay raw. */
export function LensTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const queryKey = ["cameras", camera.id, "lens"] as const;
  const lens = useQuery({ queryKey, queryFn: ({ signal }) => api<LensSettings>(`/api/cameras/${encodeURIComponent(camera.id)}/lens`, { signal }) });
  const current = useMemo(() => (lens.data ? { ...NORMAL, ...lens.data } : undefined), [lens.data]);
  const tab = useTabDraft(current, {
    label: `Lens of ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const saved = await api<LensSettings>(`/api/cameras/${encodeURIComponent(camera.id)}/lens`, { method: "PUT", body: draft });
        client.setQueryData(queryKey, saved);
        void client.invalidateQueries({ queryKey: ["cameras", "lenses"] });
        return { ok: true, base: { ...NORMAL, ...saved }, outcome: { tone: "good", text: saved.kind === "fisheye" ? "Saved. Live can dewarp this camera." : "Saved. This is a normal camera." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Lens not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (lens.isPending) return <p className="muted">Loading the lens…</p>;
  if (lens.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(lens.error, "The lens could not be loaded.")}</p>;
  const draft = tab.draft;
  const set = (change: Partial<LensSettings>) => tab.change(change);
  const number = (value: string) => (value === "" ? Number.NaN : Number(value));
  const fisheye = draft.kind === "fisheye";
  const invalid = !fisheye ? null
    : !(draft.center_x >= 0 && draft.center_x <= 1 && draft.center_y >= 0 && draft.center_y <= 1) ? "The centre is a point inside the picture: 0 to 100 %."
      : !(draft.radius >= 0.1 && draft.radius <= 1) ? "The lens circle's radius is 10 to 100 % of the picture's height."
        : !(draft.fov >= 120 && draft.fov <= 220) ? "The field of view across the circle is 120 to 220 degrees." : null;
  return (
    <form className="tab-form" aria-label="Lens" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="muted field-hint">
        A 360° camera records a round picture. Tell the NVR where the circle is and how the camera is mounted and Live can show it as a panorama, a double panorama or up to four
        virtual PTZ views (the mode menu on the tile), and an export can render one. Recordings, snapshots and playback stay the raw circle.
      </p>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Lens</legend>
        <label className="field"><span>Camera type</span>
          <select value={draft.kind} onChange={event => set({ kind: event.target.value as LensSettings["kind"] })}>
            <option value="normal">Normal camera</option>
            <option value="fisheye">360° fisheye camera</option>
          </select>
        </label>
        {fisheye ? (
          <>
            <label className="field"><span>Mount</span>
              <select value={draft.mount} onChange={event => set({ mount: event.target.value as Mount })}>
                {(Object.keys(MOUNT_LABEL) as Mount[]).map(mount => <option key={mount} value={mount}>{MOUNT_LABEL[mount]}</option>)}
              </select>
            </label>
            <label className="field"><span>Circle centre, across (%)</span>
              <input type="number" min={0} max={100} step={0.1} value={Number.isNaN(draft.center_x) ? "" : percent(draft.center_x)} onChange={event => set({ center_x: number(event.target.value) / 100 })} />
            </label>
            <label className="field"><span>Circle centre, down (%)</span>
              <input type="number" min={0} max={100} step={0.1} value={Number.isNaN(draft.center_y) ? "" : percent(draft.center_y)} onChange={event => set({ center_y: number(event.target.value) / 100 })} />
            </label>
            <label className="field"><span>Circle radius (% of the picture's height)</span>
              <input type="number" min={10} max={100} step={0.1} value={Number.isNaN(draft.radius) ? "" : percent(draft.radius)} onChange={event => set({ radius: number(event.target.value) / 100 })} />
            </label>
            <label className="field"><span>Field of view across the circle (degrees)</span>
              <input type="number" min={120} max={220} step={1} value={Number.isNaN(draft.fov) ? "" : draft.fov} onChange={event => set({ fov: number(event.target.value) })} />
            </label>
            <p className="muted field-hint">Most 360° cameras fill the picture's height with the circle, centred, and see 180°: the defaults. Choose Raw 360° on the Live tile to see the circle and check the numbers.</p>
          </>
        ) : null}
      </fieldset>
      {invalid ? <p className="muted field-hint">{invalid}</p> : null}
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={invalid} /> : null}
    </form>
  );
}
