import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";

interface Runtime {
  max_active_cameras: number;
  resource_note: string;
  model: {
    provider: string; model_id: string; model_version: string; runtime: string; available: boolean; reason: string | null; validated: boolean;
    provider_options: { id: string; available: boolean; reason: string | null }[];
    model_options: { id: string; version: string; available: boolean; source: string }[];
  };
}

/** The AI runtime: how many cameras are analysed at once, and which validated model and device. */
export function IntegrationsSettings() {
  const client = useQueryClient();
  const runtime = useQuery({ queryKey: ["ai", "runtime"], queryFn: ({ signal }) => api<Runtime>("/api/ai/settings", { signal }) });
  const published = useMemo(() => (runtime.data ? { max_active_cameras: runtime.data.max_active_cameras, provider: runtime.data.model.provider, model_id: runtime.data.model.model_id } : undefined), [runtime.data]);
  const [check, setCheck] = useState<{ busy: boolean; text?: string; tone?: "good" | "bad" }>({ busy: false });
  const tab = useTabDraft(published, {
    label: "AI runtime",
    editable: true,
    save: async (draft, base) => {
      try {
        const changedModel = draft.provider !== base.provider || draft.model_id !== base.model_id;
        const saved = await api<Runtime>("/api/ai/settings", {
          method: "PUT", body: { max_active_cameras: draft.max_active_cameras, ...(changedModel ? { provider: draft.provider, model_id: draft.model_id } : {}) },
        });
        client.setQueryData(["ai", "runtime"], saved);
        return { ok: true, base: { max_active_cameras: saved.max_active_cameras, provider: saved.model.provider, model_id: saved.model.model_id }, outcome: { tone: "good", text: changedModel ? "Validated and activated." : "Saved." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Not saved: ${failureText(error)}.` } };
      }
    },
  });

  async function validate() {
    if (!tab.draft) return;
    setCheck({ busy: true });
    try {
      const result = await api<{ valid: boolean; reason?: string | null; model_version?: string; gpu_certification_note: string }>("/api/ai/settings/validate", {
        method: "POST", body: { provider: tab.draft.provider, model_id: tab.draft.model_id },
      });
      setCheck({ busy: false, tone: result.valid ? "good" : "bad", text: result.valid ? `Valid (model ${result.model_version}). Nothing changes until you save.` : `Not valid: ${result.reason}` });
    } catch (error) {
      setCheck({ busy: false, tone: "bad", text: failureText(error) });
    }
  }

  if (!runtime.data || !tab.draft) return <p className="muted">{runtime.error ? failureText(runtime.error) : "Loading…"}</p>;
  const model = runtime.data.model;
  return (
    <form className="tab-form" aria-label="AI runtime" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice" data-tone={model.available ? undefined : "warn"}>
        Active: {model.model_id} {model.model_version} on {model.runtime}. {model.available ? "Available." : model.reason}
      </p>
      <fieldset className="field-group">
        <legend>Person and vehicle detection</legend>
        <label className="field"><span>Cameras analysed at once</span>
          <input type="number" min={1} max={16} value={tab.draft.max_active_cameras} onChange={event => tab.change({ max_active_cameras: Number(event.target.value) })} />
        </label>
        <p className="muted field-hint">{runtime.data.resource_note}</p>
        <div className="field-row">
          <label className="field"><span>Device</span>
            <select value={tab.draft.provider} onChange={event => { tab.change({ provider: event.target.value }); setCheck({ busy: false }); }}>
              {model.provider_options.map(option => <option key={option.id} value={option.id} disabled={!option.available}>{option.id}{option.available ? "" : " (unavailable)"}</option>)}
            </select>
          </label>
          <label className="field"><span>Model</span>
            <select value={tab.draft.model_id} onChange={event => { tab.change({ model_id: event.target.value }); setCheck({ busy: false }); }}>
              {model.model_options.map(option => <option key={option.id} value={option.id} disabled={!option.available}>{option.id} ({option.source}{option.available ? "" : ", unavailable"})</option>)}
            </select>
          </label>
        </div>
        <div className="field-actions">
          <button type="button" className="button" onClick={() => void validate()} disabled={check.busy}>{check.busy ? "Validating…" : "Validate"}</button>
          {check.text ? <span role={check.tone === "bad" ? "alert" : "status"} className={check.tone === "bad" ? "field-error" : "test-ok"}>{check.text}</span> : null}
        </div>
      </fieldset>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert}
                 blocked={tab.draft.max_active_cameras >= 1 && tab.draft.max_active_cameras <= 16 ? null : "Choose between 1 and 16 cameras."} />
    </form>
  );
}
