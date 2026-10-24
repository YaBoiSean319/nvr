import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/api/client";
import { useCameras } from "@/api/queries";
import type { Camera } from "@/api/types";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";

interface Draft { camera_ids: string[] }
interface Neighbours extends Draft { max: number }

const at = (id: string) => `/api/cameras/${encodeURIComponent(id)}/neighbours`;

/** The cameras an alarm on this one brings into Live with it, in order. */
export function NeighboursTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const queryKey = ["cameras", camera.id, "neighbours"] as const;
  const neighbours = useQuery({ queryKey, queryFn: ({ signal }) => api<Neighbours>(at(camera.id), { signal }) });
  const cameras = useCameras();
  const current = useMemo(() => neighbours.data ? { camera_ids: neighbours.data.camera_ids } : undefined, [neighbours.data]);
  const tab = useTabDraft(current, {
    label: `Neighbours for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const saved = await api<Neighbours>(at(camera.id), { method: "PUT", body: draft });
        client.setQueryData(queryKey, saved);
        return { ok: true, base: saved, outcome: { tone: "good", text: `Saved ${saved.camera_ids.length} neighbouring camera${saved.camera_ids.length === 1 ? "" : "s"}.` } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Neighbours not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (neighbours.isPending || cameras.isPending) return <p className="muted">Loading neighbours…</p>;
  if (neighbours.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(neighbours.error, "Neighbours could not be loaded.")}</p>;
  const chosen = tab.draft.camera_ids;
  const max = neighbours.data!.max;
  const byId = new Map((cameras.data ?? []).map(item => [item.id, item]));
  const others = (cameras.data ?? []).filter(item => item.id !== camera.id && !item.archived_at && !chosen.includes(item.id));
  const move = (index: number, offset: -1 | 1) => {
    const next = [...chosen];
    [next[index], next[index + offset]] = [next[index + offset]!, next[index]!];
    tab.change({ camera_ids: next });
  };

  return (
    <form className="tab-form" aria-label="Neighbouring cameras" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="muted field-hint">
        When a rule set to pop into Live raises an alarm on {camera.name}, Live shows {camera.name} first and then these cameras, in this order.
      </p>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Neighbours ({chosen.length} of {max})</legend>
        {chosen.length ? (
          <ol className="choice-list neighbour-list">
            {chosen.map((id, index) => (
              <li key={id}>
                <span>{byId.get(id)?.name ?? "Unknown camera"}{byId.get(id)?.archived_at ? " (archived)" : ""}</span>
                <span className="field-actions">
                  <button type="button" className="button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move ${byId.get(id)?.name ?? id} earlier`}>↑</button>
                  <button type="button" className="button" disabled={index === chosen.length - 1} onClick={() => move(index, 1)} aria-label={`Move ${byId.get(id)?.name ?? id} later`}>↓</button>
                  <button type="button" className="button" onClick={() => tab.change({ camera_ids: chosen.filter(item => item !== id) })} aria-label={`Remove ${byId.get(id)?.name ?? id}`}>Remove</button>
                </span>
              </li>
            ))}
          </ol>
        ) : <p className="muted field-hint">No neighbours: an alarm shows only {camera.name}.</p>}
        <label className="field"><span>Add a neighbour</span>
          <select value="" disabled={chosen.length >= max || !others.length} onChange={event => event.target.value && tab.change({ camera_ids: [...chosen, event.target.value] })}>
            <option value="">{others.length ? "Choose a camera…" : "No other cameras"}</option>
            {others.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
      </fieldset>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} />
    </form>
  );
}
