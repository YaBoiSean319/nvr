import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";

interface CapacityStatus {
  mode: "auto" | "fixed"; limit_cameras: number; analysed: number; wanting: number; factor: string | null; action: string | null; detail: string | null;
  cpu: number | null; cores: number | null; inference_busy: number | null; memory_used_bytes: number | null; memory_total_bytes: number | null; late_share: number | null; updated_at: string;
}

interface Runtime {
  max_active_cameras: number;
  capacity_mode: "auto" | "fixed";
  capacity: CapacityStatus | null;
  resource_note: string;
  model: {
    provider: string; model_id: string; model_version: string; runtime: string; available: boolean; reason: string | null; validated: boolean;
    provider_options: { id: string; available: boolean; reason: string | null }[];
    model_options: { id: string; version: string; available: boolean; source: string }[];
  };
}

const FACTOR = { cpu: "the CPU", inference: "the AI model's time", memory: "memory", late: "analysis falling behind" } as const;
const gigabytes = (bytes: number) => (bytes / 2 ** 30).toFixed(1);

/** What the AI worker is doing about capacity, as it last measured: how many cameras, and what is closest to its limit. */
function CapacityReport({ status, mode }: { status: CapacityStatus | null; mode: "auto" | "fixed" }) {
  if (!status) return <p className="muted" role="status">The AI worker has not reported yet.</p>;
  const measured = [
    status.cpu !== null ? `CPU ${Math.round(status.cpu * 100)}% of ${status.cores ?? "?"} cores` : null,
    status.inference_busy !== null ? `the AI model busy ${Math.round(status.inference_busy * 100)}%` : null,
    status.memory_used_bytes !== null && status.memory_total_bytes ? `memory ${gigabytes(status.memory_used_bytes)} of ${gigabytes(status.memory_total_bytes)} GB` : null,
  ].filter(Boolean).join(" · ");
  const waiting = Math.max(0, status.wanting - status.analysed);
  return (
    <div className="capacity-report" role="status" aria-label="AI capacity now">
      <p><strong>Analysing {status.analysed} of {status.wanting} camera{status.wanting === 1 ? "" : "s"}.</strong>{" "}
        {mode === "auto" && status.mode === "auto"
          ? (waiting > 0 ? `The limit is ${status.limit_cameras} for now${status.factor ? `, held by ${FACTOR[status.factor as keyof typeof FACTOR] ?? status.factor}` : ""}${status.detail ? ` (${status.detail})` : ""}.` : "Every camera that wants analysis is being analysed.")
          : mode === "fixed" && status.mode === "fixed" ? `The fixed limit is ${status.limit_cameras}.` : "This changes when you save."}
      </p>
      {measured ? <p className="muted">Measured now: {measured}.</p> : null}
      {mode === "auto" && status.mode === "auto" && waiting > 0 ? <p className="muted">It adds a camera only after the machine has had room for a minute, and takes cameras away at once when it is short of CPU, AI model time or memory. Cameras with rules, fall detection or plate reading are analysed first.</p> : null}
    </div>
  );
}

/** The AI runtime: how many cameras are analysed at once, and which validated model and device. */
export function IntegrationsSettings() {
  const client = useQueryClient();
  const runtime = useQuery({ queryKey: ["ai", "runtime"], queryFn: ({ signal }) => api<Runtime>("/api/ai/settings", { signal }) });
  // What the AI worker last measured and decided: read on its own and often, so a draft being edited is never replaced by it.
  const capacity = useQuery({ queryKey: ["ai", "capacity"], queryFn: ({ signal }) => api<Runtime>("/api/ai/settings", { signal }).then(result => result.capacity), refetchInterval: 10_000 });
  const published = useMemo(() => (runtime.data ? { max_active_cameras: runtime.data.max_active_cameras, capacity_mode: runtime.data.capacity_mode, provider: runtime.data.model.provider, model_id: runtime.data.model.model_id } : undefined), [runtime.data]);
  const [check, setCheck] = useState<{ busy: boolean; text?: string; tone?: "good" | "bad" }>({ busy: false });
  const tab = useTabDraft(published, {
    label: "AI runtime",
    editable: true,
    save: async (draft, base) => {
      try {
        const changedModel = draft.provider !== base.provider || draft.model_id !== base.model_id;
        const saved = await api<Runtime>("/api/ai/settings", {
          method: "PUT", body: { max_active_cameras: draft.max_active_cameras, capacity_mode: draft.capacity_mode, ...(changedModel ? { provider: draft.provider, model_id: draft.model_id } : {}) },
        });
        client.setQueryData(["ai", "runtime"], saved);
        return { ok: true, base: { max_active_cameras: saved.max_active_cameras, capacity_mode: saved.capacity_mode, provider: saved.model.provider, model_id: saved.model.model_id }, outcome: { tone: "good", text: changedModel ? "Validated and activated." : "Saved." } };
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
        <fieldset className="field-group" aria-label="How many cameras are analysed">
          <legend>How many cameras are analysed</legend>
          <label className="check"><input type="radio" name="capacity-mode" checked={tab.draft.capacity_mode === "auto"} onChange={() => tab.change({ capacity_mode: "auto" })} /> Automatic (recommended): as many as this machine can carry</label>
          <label className="check"><input type="radio" name="capacity-mode" checked={tab.draft.capacity_mode === "fixed"} onChange={() => tab.change({ capacity_mode: "fixed" })} /> A fixed number</label>
          {tab.draft.capacity_mode === "fixed" ? (
            <label className="field"><span>Cameras analysed at once</span>
              <input type="number" min={1} step={1} value={Number.isNaN(tab.draft.max_active_cameras) ? "" : tab.draft.max_active_cameras} onChange={event => tab.change({ max_active_cameras: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
            </label>
          ) : null}
          <CapacityReport status={capacity.data ?? runtime.data.capacity} mode={tab.draft.capacity_mode === runtime.data.capacity_mode ? runtime.data.capacity_mode : tab.draft.capacity_mode} />
        </fieldset>
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
                 blocked={tab.draft.capacity_mode === "auto" || (Number.isInteger(tab.draft.max_active_cameras) && tab.draft.max_active_cameras >= 1) ? null : "Choose a whole number of cameras, 1 or more."} />
    </form>
  );
}
