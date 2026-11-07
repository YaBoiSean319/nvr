import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/api/client";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";

type Mode = "off" | "recent" | "all";
interface LiveReady {
  mode: Mode;
  choices: { id: Mode; label: string; description: string }[];
  held: { camera_id: string; name: string; profile: string; state: "starting" | "ready" | "failed" }[];
  limit: number;
}

const STATE_TEXT = { starting: "starting", ready: "ready", failed: "not answering, trying again" } as const;

/** How soon live video starts: whether the NVR keeps cameras' live streams open ahead of anyone asking (#216). */
export function LiveReadySettings() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings", "live-ready"], queryFn: ({ signal }) => api<LiveReady>("/api/live-ready", { signal }), refetchInterval: 15_000 });
  const published = useMemo(() => (query.data ? { mode: query.data.mode } : undefined), [query.data]);
  const tab = useTabDraft(published, {
    label: "Live video",
    editable: true,
    save: async (draft, base) => {
      try {
        const saved = await api<LiveReady>("/api/live-ready", { method: "PUT", body: draft });
        client.setQueryData(["settings", "live-ready"], saved);
        return { ok: true, base: { mode: saved.mode }, outcome: { tone: "good", text: "Saved." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (!tab.draft || !query.data) return <p className="muted">{query.error ? failureText(query.error) : "Loading…"}</p>;
  const draft = tab.draft;
  const held = query.data.held;
  return (
    <form className="tab-form" aria-label="Live video" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <fieldset className="field-group">
        <legend>Live video start-up</legend>
        <p className="muted field-hint">Opening a camera that nobody is watching makes the NVR connect to it and wait for it to start sending, usually 2 to 6 seconds. Keeping the stream open ahead of time takes that wait away.</p>
        <div role="radiogroup" aria-label="Keep live video ready" className="stack">
          {query.data.choices.map(choice => (
            <label key={choice.id} className="field-radio">
              <input type="radio" name="live-ready-mode" checked={draft.mode === choice.id} onChange={() => tab.change({ mode: choice.id })} />{" "}
              <strong>{choice.label}</strong><br />
              <span className="muted">{choice.description}</span>
            </label>
          ))}
        </div>
        {query.data.mode === "all" ? (
          held.length ? (
            <details>
              <summary>{held.length} camera{held.length === 1 ? "" : "s"} kept ready{held.length >= query.data.limit ? ` (the most that are held: ${query.data.limit})` : ""}</summary>
              <ul className="plain-list" aria-label="Streams kept ready">
                {held.map(item => <li key={`${item.camera_id}-${item.profile}`}>{item.name || item.camera_id} · {item.profile === "sub" ? "substream" : "main stream"} · {STATE_TEXT[item.state]}</li>)}
              </ul>
            </details>
          ) : <p className="muted">No camera is being held yet.</p>
        ) : null}
      </fieldset>
      <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={null} />
    </form>
  );
}
