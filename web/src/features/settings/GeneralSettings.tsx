import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { formatRelative } from "@/lib/time";
import { api } from "@/api/client";
import type { DisplaySettings } from "@/api/types";
import { TabFooter } from "@/features/admin/TabFooter";
import { failureText, useTabDraft } from "@/features/admin/tabDraft";

const DATE_FORMATS: { value: DisplaySettings["date_format"]; label: string }[] = [
  { value: "friendly", label: "Oct 21, 2026" }, { value: "long", label: "October 21, 2026" }, { value: "iso", label: "2026-10-21" },
  { value: "day-month", label: "21/10/2026" }, { value: "month-day", label: "10/21/2026" },
];

const ZONES: string[] = (() => {
  try {
    return (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  } catch {
    return [];
  }
})();

interface SystemStatus {
  version: string;
  application_status: string;
  workers: { name: string; mode: "process" | "in_process"; alive: boolean; state?: string; restarts?: number }[];
  configured_administrators: number;
  configured_cameras: number;
  self_test: { passed: boolean; finished_at: string; results: { passed: boolean | null }[] } | null;
}

/** Site date/time display with a server-rendered preview, and the system's own status. */
export function GeneralSettings() {
  const client = useQueryClient();
  const display = useQuery({ queryKey: ["settings", "display"], queryFn: ({ signal }) => api<DisplaySettings>("/api/settings/display", { signal }) });
  const system = useQuery({ queryKey: ["system", "status"], queryFn: ({ signal }) => api<SystemStatus>("/api/system/status", { signal }), refetchInterval: 15_000 });
  const published = useMemo(() => (display.data ? { time_zone: display.data.time_zone, date_format: display.data.date_format, clock_format: display.data.clock_format } : undefined), [display.data]);
  const tab = useTabDraft(published, {
    label: "Date and time display",
    editable: true,
    save: async (draft, base) => {
      try {
        const saved = await api<DisplaySettings>("/api/settings/display", { method: "PUT", body: draft });
        client.setQueryData(["settings", "display"], saved);
        return { ok: true, base: { time_zone: saved.time_zone, date_format: saved.date_format, clock_format: saved.clock_format }, outcome: { tone: "good", text: "Saved. Times everywhere, including new exports, use these settings." } };
      } catch (error) {
        return { ok: false, base, outcome: { tone: "bad", text: `Not saved: ${failureText(error)}.` } };
      }
    },
  });
  const [preview, setPreview] = useState<{ text?: string; error?: string }>({});

  // The preview is rendered by the server, exactly as exports and the timeline will show it.
  useEffect(() => {
    if (!tab.draft) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api<{ preview: string }>("/api/settings/display/preview", { method: "POST", body: tab.draft, signal: controller.signal })
        .then(result => setPreview({ text: result.preview }))
        .catch(error => !controller.signal.aborted && setPreview({ error: failureText(error) }));
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [tab.draft]);

  if (!tab.draft) return <p className="muted">Loading…</p>;
  const draft = tab.draft;
  return (
    <>
      <form className="tab-form" aria-label="Date and time display" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
        <fieldset className="field-group">
          <legend>Site date and time</legend>
          <label className="field"><span>Time zone</span>
            <input list="settings-zones" value={draft.time_zone} autoComplete="off" onChange={event => tab.change({ time_zone: event.target.value })} />
            <datalist id="settings-zones">{ZONES.map(zone => <option key={zone} value={zone} />)}</datalist>
          </label>
          <label className="field"><span>Date format</span>
            <select value={draft.date_format} onChange={event => tab.change({ date_format: event.target.value as DisplaySettings["date_format"] })}>
              {DATE_FORMATS.map(format => <option key={format.value} value={format.value}>{format.label}</option>)}
            </select>
          </label>
          <div className="segmented" role="radiogroup" aria-label="Clock">
            <label><input type="radio" checked={draft.clock_format === "12"} onChange={() => tab.change({ clock_format: "12" })} /> 12-hour</label>
            <label><input type="radio" checked={draft.clock_format === "24"} onChange={() => tab.change({ clock_format: "24" })} /> 24-hour</label>
          </div>
          <p className="field-hint" aria-live="polite">
            {preview.error ? <span className="field-error">{preview.error}</span> : preview.text ? <>Preview: <strong data-testid="display-preview">{preview.text}</strong></> : "Preview loading…"}
          </p>
        </fieldset>
        <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={preview.error ?? null} />
      </form>
      <section className="field-group settings-block" aria-label="System status">
        <h2 className="field-heading">System status</h2>
        {system.data ? (
          <>
            <p>Version {system.data.version} · {system.data.application_status === "healthy" ? "All workers running" : "Degraded"} · {system.data.configured_cameras} cameras · {system.data.configured_administrators} administrators</p>
            <ul className="plain-list">
              {system.data.workers.map(worker => <li key={worker.name}>{worker.name}: {worker.alive ? "running" : "not running"}{worker.state ? ` · ${worker.state}` : ""}{worker.restarts ? ` · restarted ${worker.restarts} time${worker.restarts === 1 ? "" : "s"}` : ""}</li>)}
            </ul>
            <p data-testid="self-test-summary">
              {system.data.self_test
                ? `Self-test ${system.data.self_test.passed ? "passed" : `failed ${system.data.self_test.results.filter(result => result.passed === false).length} check(s)`} ${formatRelative(system.data.self_test.finished_at)}.`
                : "The self-test has not run yet."}{" "}
              <Link to="/settings/coverage">Coverage &amp; self-test</Link>
            </p>
          </>
        ) : <p className="muted">{system.error ? failureText(system.error) : "Loading…"}</p>}
      </section>
    </>
  );
}
