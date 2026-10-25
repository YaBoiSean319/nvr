import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import type { Camera } from "@/api/types";
import { formatRelative } from "@/lib/time";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { getTamper, putTamper, relearnTamper, type CameraTamper, type TamperState } from "./api";

interface Draft { enabled: boolean; sensitivity: number; sustain_seconds: number }

const fromServer = (config: CameraTamper): Draft => ({ enabled: config.enabled, sensitivity: config.sensitivity, sustain_seconds: config.sustain_seconds });

const STATE_TEXT: Record<TamperState, string> = {
  off: "Off",
  learning: "Learning the view",
  ok: "Normal",
  covered: "Covered",
  moved: "Moved",
  defocused: "Defocused",
};

/** Whether the camera is covered, moved or defocused: how sensitive to be, how long a change must last, and learning the view again. */
export function TamperTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const queryKey = ["cameras", camera.id, "tamper"] as const;
  const config = useQuery({ queryKey, queryFn: ({ signal }) => getTamper(camera.id, signal), refetchInterval: 15_000 });
  const current = useMemo(() => (config.data ? fromServer(config.data) : undefined), [config.data]);
  const tab = useTabDraft(current, {
    label: `Tamper detection for ${camera.name}`,
    editable: admin,
    save: async (draft, base) => {
      try {
        const saved = await putTamper(camera.id, draft);
        client.setQueryData(queryKey, saved);
        return { ok: true, base: fromServer(saved), outcome: { tone: "good", text: saved.enabled ? "Saved. Tamper detection is on." : "Saved. Tamper detection is off." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Tamper settings not saved: ${failureText(error)}.` } };
      }
    },
  });
  const relearn = useMutation({
    mutationFn: () => relearnTamper(camera.id),
    onSuccess: saved => {
      client.setQueryData(queryKey, saved);
      void client.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
  if (config.isPending) return <p className="muted">Loading tamper detection…</p>;
  if (config.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(config.error, "Tamper detection could not be loaded.")}</p>;
  const draft = tab.draft;
  const data = config.data;
  const readOnly = !admin;
  const invalid = !(draft.sensitivity >= 1 && draft.sensitivity <= 100) ? "Sensitivity is a number from 1 to 100."
    : !(draft.sustain_seconds >= 10 && draft.sustain_seconds <= 3600) ? "A change must last between 10 and 3600 seconds." : null;
  const tampered = data.state === "covered" || data.state === "moved" || data.state === "defocused";
  return (
    <form className="tab-form" aria-label="Tamper detection" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="muted field-hint">
        The recorder looks at a small frame from this camera every {data.sample_seconds} seconds and compares it with a view it has learned: a covered lens, a camera turned or knocked,
        or a lens out of focus raises an incident once it has lasted long enough. Gradual changes of light, such as dusk, are followed and raise nothing.
      </p>
      <p className="notice" role="note" aria-live="polite" data-tone={tampered ? "bad" : data.state === "ok" ? "good" : undefined}>
        {STATE_TEXT[data.state]}
        {data.state === "learning" ? ` · ${data.learning_progress} of ${data.learning_samples} frames` : ""}
        {tampered && data.state_since ? ` · since ${formatRelative(data.state_since)}` : ""}
        {data.enabled && data.last_sample_at ? ` · last looked ${formatRelative(data.last_sample_at)}` : ""}
        {data.reference_at ? ` · view learned ${formatRelative(data.reference_at)}` : ""}
      </p>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Detection</legend>
        <label className="check"><input type="checkbox" checked={draft.enabled} onChange={event => tab.change({ enabled: event.target.checked })} /> Detect a covered, moved or defocused camera</label>
        <fieldset className="plain" disabled={readOnly || !draft.enabled}>
          <div className="field-row">
            <label className="field"><span>Sensitivity (1–100)</span>
              <input type="number" min={1} max={100} value={Number.isNaN(draft.sensitivity) ? "" : draft.sensitivity}
                     onChange={event => tab.change({ sensitivity: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
            </label>
            <label className="field"><span>A change must last (seconds)</span>
              <input type="number" min={10} max={3600} value={Number.isNaN(draft.sustain_seconds) ? "" : draft.sustain_seconds}
                     onChange={event => tab.change({ sustain_seconds: event.target.value === "" ? Number.NaN : Number(event.target.value) })} />
            </label>
          </div>
          <p className="muted field-hint">A higher sensitivity raises an incident for a smaller change. Rules can act on a detection: choose “Camera is covered, moved or defocused” as a rule's condition.</p>
        </fieldset>
      </fieldset>
      {admin ? (
        <section className="field-group" aria-label="Learn the view again">
          <h3 className="field-heading">Learn the view again</h3>
          <p className="muted field-hint">After you have moved the camera or rearranged what it watches on purpose, learn the new view. This ends any detection now.</p>
          {relearn.error ? <p className="notice" data-tone="bad" role="alert">{failureText(relearn.error)}</p> : null}
          <div className="field-actions">
            <button type="button" className="button" disabled={relearn.isPending || !data.enabled} onClick={() => relearn.mutate()}>{relearn.isPending ? "Starting…" : "Re-learn reference"}</button>
          </div>
        </section>
      ) : null}
      {admin && invalid && draft.enabled ? <p className="notice" data-tone="warn" role="alert">{invalid}</p> : null}
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} /> : null}
    </form>
  );
}
