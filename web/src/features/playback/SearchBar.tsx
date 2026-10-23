import { useState, type FormEvent } from "react";
import type { DisplaySettings } from "@/api/types";
import { localTimeCandidates, offsetLabel } from "@/lib/zonedTime";
import { sameDraft, usePlaybackStore, type SearchDraft } from "./store";

/** The server bounds one search to 24 hours. */
export const MAX_SEARCH_MS = 24 * 3_600_000;

type Field = "start" | "end";
const LABEL: Record<Field, string> = { start: "From", end: "To" };

type Resolution = { instant: number } | { error: string } | { choose: number[] };

function resolveField(draft: SearchDraft, field: Field, zone: string): Resolution {
  const value = draft[field];
  if (!value) return { error: `Enter a ${LABEL[field]} time.` };
  const candidates = localTimeCandidates(value, zone);
  if (!candidates.length) return { error: `${LABEL[field]} ${value.replace("T", " ")} does not exist in ${zone} because the clocks jump forward. Choose another time.` };
  if (candidates.length === 1) return { instant: candidates[0]! };
  const chosen = Number(draft[field === "start" ? "startFold" : "endFold"]);
  return candidates.includes(chosen) ? { instant: chosen } : { choose: candidates };
}

export function resolveSearch(draft: SearchDraft, zone: string): { start: number; end: number } | { error: string } {
  const start = resolveField(draft, "start", zone), end = resolveField(draft, "end", zone);
  for (const [field, result] of [["start", start], ["end", end]] as const) {
    if ("error" in result) return result;
    if ("choose" in result) return { error: `${LABEL[field]} ${draft[field].replace("T", " ")} happens twice in ${zone}. Choose which occurrence to use.` };
  }
  const from = (start as { instant: number }).instant, to = (end as { instant: number }).instant;
  if (to <= from) return { error: "To must be later than From." };
  if (to - from > MAX_SEARCH_MS) return { error: "A search covers at most 24 hours. Narrow the range." };
  return { start: from, end: to };
}

export function SearchBar({ settings }: { settings: DisplaySettings }) {
  const draft = usePlaybackStore(store => store.draft);
  const applied = usePlaybackStore(store => store.applied);
  const { setDraft, apply } = usePlaybackStore.getState();
  const [error, setError] = useState<string | null>(null);
  const zone = settings.time_zone;
  if (!draft) return null;
  const stale = Boolean(applied) && !sameDraft(draft, applied!.draft);

  function submit(event: FormEvent) {
    event.preventDefault();
    const result = resolveSearch(draft!, zone);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setError(null);
    apply({ start: new Date(result.start).toISOString(), end: new Date(result.end).toISOString(), draft: draft! });
  }

  function field(name: Field) {
    const resolution = draft![name] ? resolveField(draft!, name, zone) : null;
    const foldKey = name === "start" ? "startFold" : "endFold";
    const choices = resolution && "choose" in resolution ? resolution.choose
      : resolution && "instant" in resolution && localTimeCandidates(draft![name], zone).length > 1 ? localTimeCandidates(draft![name], zone) : null;
    return (
      <div className="search-field">
        <label>
          <span>{LABEL[name]}</span>
          <input type="datetime-local" value={draft![name]} required
                 onChange={event => setDraft({ [name]: event.target.value, [foldKey]: "" })} />
        </label>
        {choices ? (
          <select aria-label={`Which ${LABEL[name]} occurrence`} value={draft![foldKey]} onChange={event => setDraft({ [foldKey]: event.target.value })}>
            <option value="">Choose occurrence…</option>
            {choices.map((instant, index) => (
              <option key={instant} value={String(instant)}>{index === 0 ? "First" : "Second"} · {offsetLabel(instant, zone)}</option>
            ))}
          </select>
        ) : null}
        {resolution && "error" in resolution && draft![name] ? <span className="field-error" role="alert">Does not exist</span> : null}
      </div>
    );
  }

  return (
    <form className="search-bar" onSubmit={submit} aria-label="Footage search" data-stale={stale ? "true" : "false"} noValidate>
      {field("start")}
      {field("end")}
      <span className="muted search-zone" title="Search times use the site time zone">{zone}</span>
      <button type="submit" className="button" data-variant={!applied || stale ? "primary" : undefined}>Search</button>
      {stale ? <span className="search-stale" role="status">Search changed — press Search to apply</span> : null}
      {error ? <p className="search-error" role="alert">{error}</p> : null}
    </form>
  );
}
