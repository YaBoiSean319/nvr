import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import type { Camera } from "@/api/types";
import { formatRelative } from "@/lib/time";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { getAi, getAiCoverage, putAi, type CameraAi, type Zone } from "./api";
import { ZoneEditor } from "./ZoneEditor";

interface Draft {
  inherit_group: boolean;
  enabled: boolean;
  analysis_fps: number;
  confidence_threshold: number;
  use_substream: boolean;
  zones: Zone[];
}

const fromServer = (config: CameraAi, hasGroup: boolean): Draft => ({
  inherit_group: hasGroup && !config.config_override,
  enabled: config.enabled, analysis_fps: config.analysis_fps, confidence_threshold: config.confidence_threshold,
  use_substream: config.use_substream, zones: config.zones,
});

/** Person and vehicle detection for one camera: inherit the group or override it. */
export function AiTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const queryKey = ["cameras", camera.id, "ai"] as const;
  const config = useQuery({ queryKey, queryFn: ({ signal }) => getAi(camera.id, signal) });
  const coverage = useQuery({ queryKey: ["cameras", camera.id, "ai-coverage"], queryFn: ({ signal }) => getAiCoverage(camera.id, signal) });
  const hasGroup = Boolean(camera.group_name);
  const current = useMemo(() => (config.data ? fromServer(config.data, hasGroup) : undefined), [config.data, hasGroup]);
  const tab = useTabDraft(current, {
    label: `AI for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const saved = await putAi(camera.id, draft);
        client.setQueryData(queryKey, saved);
        return { ok: true, base: fromServer(saved, hasGroup), outcome: { tone: "good", text: `Saved AI settings (${saved.config_source}). Status: ${saved.status}.` } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `AI settings not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (config.isPending) return <p className="muted">Loading AI settings…</p>;
  if (config.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(config.error, "AI settings could not be loaded.")}</p>;
  const draft = tab.draft;
  const data = config.data!;
  const readOnly = !admin;
  const inherited = draft.inherit_group;
  return (
    <form className="tab-form" aria-label="AI settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice" data-tone={data.model.available ? undefined : "warn"}>
        Model {data.model.model_id} {data.model.model_version} on {data.model.runtime}: {data.model.available ? "available" : data.model.reason ?? "unavailable"}.
        {" "}Settings come from: {data.config_source}. Status: {data.status}{data.last_error ? ` · ${data.last_error}` : ""}.
      </p>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Detection</legend>
        {hasGroup ? (
          <label className="check"><input type="checkbox" checked={inherited} onChange={event => tab.change({ inherit_group: event.target.checked })} /> Use the {camera.group_name} group's AI settings</label>
        ) : null}
        <fieldset className="plain" disabled={readOnly || inherited}>
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => tab.change({ enabled: event.target.checked })} /> Detect people and vehicles</label>
          <div className="field-row">
            <label className="field"><span>Frames analysed per second</span>
              <input type="number" min={0.1} max={5} step={0.1} value={draft.analysis_fps} onChange={event => tab.change({ analysis_fps: Number(event.target.value) })} />
            </label>
            <label className="field"><span>Confidence threshold</span>
              <input type="number" min={0.1} max={0.99} step={0.01} value={draft.confidence_threshold} onChange={event => tab.change({ confidence_threshold: Number(event.target.value) })} />
            </label>
          </div>
          <label className="check"><input type="checkbox" checked={draft.use_substream} onChange={event => tab.change({ use_substream: event.target.checked })} /> Analyse the lower-resolution substream</label>
        </fieldset>
      </fieldset>
      <ZoneEditor cameraId={camera.id} zones={draft.zones} onChange={zones => tab.change({ zones })} label="AI zones" readOnly={readOnly || inherited} />
      <section className="field-group" aria-label="AI coverage">
        <h3 className="field-heading">Coverage</h3>
        <p className="muted field-hint">{data.coverage_note}</p>
        {coverage.data?.intervals.length ? (
          <ul className="plain-list">
            {coverage.data.intervals.slice(0, 8).map(interval => (
              <li key={`${interval.starts_at}-${interval.state}`}>{interval.state} · from {formatRelative(interval.starts_at)}{interval.ends_at ? ` to ${formatRelative(interval.ends_at)}` : " (ongoing)"}{interval.detail ? ` · ${interval.detail}` : ""}</li>
            ))}
          </ul>
        ) : <p className="muted">No AI coverage has been recorded for this camera.</p>}
      </section>
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} /> : null}
    </form>
  );
}
