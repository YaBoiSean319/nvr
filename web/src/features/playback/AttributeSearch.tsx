import { useQuery } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { api } from "@/api/client";
import type { DisplaySettings } from "@/api/types";
import { failureText } from "@/features/admin/tabDraft";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { usePlaybackStore } from "./store";

interface Options { labels: string[]; colours: string[]; vehicle_types: string[]; directions: string[]; vehicle_type_available: boolean; zones: Record<string, unknown>; max_range_days: number }
interface Result {
  track_id: string; camera_id: string; camera_name: string; label: string; first_seen_at: string; last_seen_at: string; confidence: number;
  attributes: Record<string, string>; thumbnail_url: string | null; box: { x: number; y: number; width: number; height: number } | null;
}
interface SimilarResult extends Result { similarity: number; rank: number }
interface SimilarResponse { track_id: string; results: SimilarResult[]; coverage: Coverage }
interface AskResult extends Result { score: number | null; rank: number }
interface AskResponse {
  query: string; mode: "image-text" | "attributes" | "browse"; mode_note: string; results: AskResult[]; coverage: Coverage; coverage_note: string; unindexed: number; truncated: boolean; not_yet: boolean;
  interpretation: { text: string; label: string | null; attributes: Record<string, string | string[]>; cameras: { id: string; name: string }[]; range: { start: string; end: string } | null; time_of_day: { from: string; to: string } | null };
}
interface Interval { state: string; starts_at: string; ends_at: string; detail: string; seconds: number }
interface CameraCoverage { camera_id: string; camera_name: string; analysed_seconds: number; unanalysed_seconds: number; intervals: Interval[]; more_intervals: number }
interface Coverage { range_seconds: number; searched_cameras: number; analysed_percent: number | null; fully_analysed: boolean; cameras: CameraCoverage[] }
interface Response { start: string; end: string; results: Result[]; truncated: boolean; coverage: Coverage; coverage_note: string }

interface Filters { label: string; upper: string; lower: string; vehicleColour: string; vehicleType: string; direction: string; zone: string; cameras: "all" | "view"; when: string }
const EMPTY: Filters = { label: "", upper: "", lower: "", vehicleColour: "", vehicleType: "", direction: "", zone: "", cameras: "all", when: "24h" };

const DIRECTION_LABEL: Record<string, string> = { left: "Moving left", right: "Moving right", toward: "Toward the camera", away: "Away from the camera", stationary: "Not moving" };
const ZONE_LABEL: Record<string, string> = { left: "Left half", right: "Right half", top: "Top half", bottom: "Bottom half", centre: "Centre" };
const STATE_LABEL: Record<string, string> = { not_running: "AI not running", failed: "Analysis failed", delayed: "Analysis delayed", unanalyzed: "Not analysed", interrupted: "Analysis interrupted", skipped: "Skipped" };
const WHEN: Record<string, { label: string; hours: number }> = {
  "1h": { label: "Last hour", hours: 1 }, "24h": { label: "Last 24 hours", hours: 24 }, "7d": { label: "Last 7 days", hours: 168 }, "30d": { label: "Last 30 days", hours: 720 },
};

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

function Select({ label, value, onChange, children, disabled, hint }: { label: string; value: string; onChange: (value: string) => void; children: React.ReactNode; disabled?: boolean; hint?: string }) {
  const hintId = useId();
  return (
    <label className="attr-field"><span>{label}</span>
      <select value={value} disabled={disabled} aria-describedby={hint ? hintId : undefined} onChange={event => onChange(event.target.value)}>{children}</select>
      {hint ? <span id={hintId} className="muted">{hint}</span> : null}
    </label>
  );
}

function summary(result: Result): string {
  const { attributes } = result;
  const parts: string[] = [];
  if (result.label === "person") {
    if (attributes.upper_colour) parts.push(`${attributes.upper_colour} top`);
    if (attributes.lower_colour) parts.push(`${attributes.lower_colour} lower`);
  } else {
    parts.push([attributes.vehicle_colour, attributes.vehicle_type ?? "vehicle"].filter(Boolean).join(" "));
  }
  if (attributes.direction) parts.push(DIRECTION_LABEL[attributes.direction]?.toLowerCase() ?? attributes.direction);
  return parts.join(", ");
}

function ResultCard({ result, when, onOpen, similarity, score }: { result: Result; when: (iso: string) => string; onOpen: (result: Result) => void; similarity?: number; score?: number | null }) {
  const scored = score === null || score === undefined ? null : `score ${Math.round(score * 100)}`;
  return (
    <button type="button" className="attr-card" onClick={() => onOpen(result)} aria-label={`Open ${result.camera_name} in Playback at ${when(result.first_seen_at)}: ${summary(result)}${similarity === undefined ? "" : `, ${Math.round(similarity * 100)}% similar`}${scored ? `, ${scored}` : ""}`}>
      <span className="attr-thumb">
        {result.thumbnail_url ? <img src={result.thumbnail_url} alt="" loading="lazy" /> : <span className="attr-nopicture">No picture</span>}
        {result.box && result.thumbnail_url ? <span className="attr-box" style={{ left: `${result.box.x * 100}%`, top: `${result.box.y * 100}%`, width: `${result.box.width * 100}%`, height: `${result.box.height * 100}%` }} /> : null}
      </span>
      <span className="attr-meta"><strong>{result.camera_name}</strong><span>{when(result.first_seen_at)}</span><span>{summary(result)}</span>{similarity === undefined ? null : <span className="attr-score">{Math.round(similarity * 100)}% similar</span>}{scored ? <span className="attr-score">{scored}</span> : null}</span>
    </button>
  );
}

/** Find people and vehicles across cameras by what they looked like and how they moved; each result opens Playback at that moment. */
export function AttributeSearch({ settings, range, shownIds }: { settings: DisplaySettings; range: { start: number; end: number } | null; shownIds: string[] }) {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [submitted, setSubmitted] = useState<Filters | null>(null);
  const [similarTo, setSimilarTo] = useState<Result | null>(null);
  const [description, setDescription] = useState("");
  const [asked, setAsked] = useState<{ text: string; sequence: number } | null>(null);
  const options = useQuery({ queryKey: ["ai-search-options"], queryFn: ({ signal }) => api<Options>("/api/ai/search/options", { signal }), staleTime: 60 * 60_000 });
  const when = (iso: string) => formatSiteDateTime(Date.parse(iso), settings);
  const change = (patch: Partial<Filters>) => setFilters(current => ({ ...current, ...patch }));

  const found = useQuery({
    queryKey: ["ai-search", submitted, submitted?.cameras === "view" ? shownIds : null, submitted?.when === "range" ? range : null],
    enabled: submitted !== null,
    queryFn: ({ signal }) => {
      const query = submitted!;
      const end = query.when === "range" && range ? range.end : Date.now();
      const start = query.when === "range" && range ? range.start : end - (WHEN[query.when]?.hours ?? 24) * 3_600_000;
      const params = new URLSearchParams({ start: new Date(start).toISOString(), end: new Date(end).toISOString() });
      const set = (name: string, value: string) => value && params.set(name, value);
      set("label", query.label);
      set("upper_colour", query.label === "vehicle" ? "" : query.upper);
      set("lower_colour", query.label === "vehicle" ? "" : query.lower);
      set("vehicle_colour", query.label === "person" ? "" : query.vehicleColour);
      set("vehicle_type", query.label === "person" ? "" : query.vehicleType);
      set("direction", query.direction);
      set("zone", query.zone);
      if (query.cameras === "view") shownIds.forEach(id => params.append("camera_ids", id));
      return api<Response>(`/api/ai/search?${params}`, { signal });
    },
  });

  const alike = useQuery({
    queryKey: ["ai-similar", similarTo?.track_id],
    enabled: similarTo !== null,
    queryFn: ({ signal }) => api<SimilarResponse>(`/api/ai/tracks/${similarTo!.track_id}/similar`, { signal }),
  });

  const answer = useQuery({
    queryKey: ["ai-ask", asked],
    enabled: asked !== null,
    // The site's own time zone decides what "6 pm" and "yesterday" mean; asking again always asks again (a question about "the last hour" goes stale).
    queryFn: ({ signal }) => api<AskResponse>(`/api/ai/search/ask?${new URLSearchParams({ q: asked!.text })}`, { signal }),
    staleTime: 0,
    retry: false,
  });

  function ask(event: FormEvent) {
    event.preventDefault();
    if (description.trim()) setAsked(current => ({ text: description.trim(), sequence: (current?.sequence ?? 0) + 1 }));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted({ ...filters });
  }
  function open(result: Result) {
    usePlaybackStore.getState().openInPlayback(result.camera_id, Date.parse(result.first_seen_at));
  }

  const data = found.data;
  const people = filters.label !== "vehicle", vehicles = filters.label !== "person";
  const colours = options.data?.colours ?? [];
  const noCameras = filters.cameras === "view" && shownIds.length === 0;
  return (
    <aside className="export-drawer" id="attribute-search" aria-label="Attribute search">
      <header className="export-head">
        <h2>Find by attributes</h2>
        <button type="button" className="button icon-button" data-variant="ghost" aria-label="Close attribute search" onClick={() => usePlaybackStore.getState().setAttributeSearchOpen(false)}>✕</button>
      </header>
      <div className="export-body attr-body">
        <form className="attr-form attr-ask" onSubmit={ask} role="search" aria-label="Describe what you are looking for" noValidate>
          <label className="attr-field"><span>Describe what you are looking for</span>
            <input type="search" value={description} maxLength={300} placeholder="white van at the gate after 6 pm" onChange={event => setDescription(event.target.value)} />
            <span className="muted">Time (“yesterday”, “after 6 pm”), camera or camera group names (“at the gate”) and parts of the frame (“in the left half”) narrow the search; other place words are ignored. Nothing leaves this server.</span>
          </label>
          <div className="attr-actions"><button type="submit" className="button" data-variant="primary" disabled={!description.trim() || answer.isFetching}>{answer.isFetching ? "Looking…" : "Ask"}</button></div>
        </form>
        {answer.error ? <p className="notice" data-tone="bad" role="alert">{failureText(answer.error)}</p> : null}
        {answer.data ? (
          <section className="attr-answer" aria-label="Description results">
            <p className="muted" role="status">
              {answer.data.not_yet ? "The time asked for has not happened yet." : answer.data.results.length ? `${answer.data.results.length} result${answer.data.results.length === 1 ? "" : "s"}, best first.` : "Nothing matched in the time that was analysed."}{" "}
              {answer.data.truncated ? "More objects than can be ranked at once were in range: ask about a shorter time or one camera. " : ""}
              {answer.data.interpretation.cameras.length ? `Cameras: ${answer.data.interpretation.cameras.map(camera => camera.name).join(", ")}. ` : ""}
              {answer.data.interpretation.time_of_day ? `Time of day: ${answer.data.interpretation.time_of_day.from} to ${answer.data.interpretation.time_of_day.to}. ` : ""}
              {answer.data.interpretation.range ? `From ${when(answer.data.interpretation.range.start)} to ${when(answer.data.interpretation.range.end)}. ` : "Last 7 days. "}
              {answer.data.mode_note}
              {answer.data.unindexed ? ` ${answer.data.unindexed} object${answer.data.unindexed === 1 ? " was" : "s were"} analysed before the image-text model was installed and rank only by their attributes.` : ""}
            </p>
            <p className="notice" role="status" data-tone={answer.data.coverage.fully_analysed ? "good" : "warn"}>
              {answer.data.coverage.searched_cameras === 0 ? "No cameras were searched."
                : answer.data.coverage.analysed_percent === null ? "Nothing to check: no time in range has happened yet."
                : answer.data.coverage.fully_analysed ? `Every searched camera (${answer.data.coverage.searched_cameras}) was analysed for the whole range.`
                : `${answer.data.coverage.analysed_percent}% of the searched time was analysed. ${answer.data.coverage.cameras.map(camera => `${camera.camera_name}: ${duration(camera.unanalysed_seconds)} not analysed`).join("; ")}. No match in time that was not analysed means it was not looked at.`}
            </p>
            <ul className="attr-grid" aria-label="Description matches">
              {answer.data.results.map(result => (
                <li key={result.track_id}><ResultCard result={result} when={when} onOpen={open} score={result.score} /></li>
              ))}
            </ul>
          </section>
        ) : null}

        <form className="attr-form" onSubmit={submit} noValidate>
          <Select label="Kind" value={filters.label} onChange={label => change({ label })}>
            <option value="">Person or vehicle</option><option value="person">Person</option><option value="vehicle">Vehicle</option>
          </Select>
          {people ? (
            <>
              <Select label="Top colour" value={filters.upper} onChange={upper => change({ upper })}><option value="">Any</option>{colours.map(name => <option key={name} value={name}>{name}</option>)}</Select>
              <Select label="Lower colour" value={filters.lower} onChange={lower => change({ lower })}><option value="">Any</option>{colours.map(name => <option key={name} value={name}>{name}</option>)}</Select>
            </>
          ) : null}
          {vehicles ? (
            <>
              <Select label="Vehicle colour" value={filters.vehicleColour} onChange={vehicleColour => change({ vehicleColour })}><option value="">Any</option>{colours.map(name => <option key={name} value={name}>{name}</option>)}</Select>
              <Select label="Vehicle type" value={filters.vehicleType} onChange={vehicleType => change({ vehicleType })} disabled={options.data ? !options.data.vehicle_type_available : false}
                      hint={options.data && !options.data.vehicle_type_available ? "Needs the vehicle attribute model, which is not installed." : undefined}>
                <option value="">Any</option>{(options.data?.vehicle_types ?? []).map(name => <option key={name} value={name}>{name}</option>)}
              </Select>
            </>
          ) : null}
          <Select label="Direction" value={filters.direction} onChange={direction => change({ direction })}>
            <option value="">Any</option>{(options.data?.directions ?? []).map(name => <option key={name} value={name}>{DIRECTION_LABEL[name] ?? name}</option>)}
          </Select>
          <Select label="Passed through" value={filters.zone} onChange={zone => change({ zone })}>
            <option value="">Anywhere in view</option>{Object.keys(options.data?.zones ?? {}).map(name => <option key={name} value={name}>{ZONE_LABEL[name] ?? name}</option>)}
          </Select>
          <Select label="Cameras" value={filters.cameras} onChange={cameras => change({ cameras: cameras as Filters["cameras"] })}>
            <option value="all">All cameras I can see</option><option value="view">Cameras in this Playback view</option>
          </Select>
          <Select label="When" value={filters.when} onChange={next => change({ when: next })}>
            {Object.entries(WHEN).map(([key, item]) => <option key={key} value={key}>{item.label}</option>)}
            {range ? <option value="range">The Playback search range</option> : null}
          </Select>
          <div className="attr-actions">
            <button type="submit" className="button" data-variant="primary" disabled={noCameras || found.isFetching}>{found.isFetching ? "Searching…" : "Search"}</button>
            <button type="button" className="button" data-variant="ghost" onClick={() => { setFilters(EMPTY); setSubmitted(null); }}>Clear</button>
          </div>
          {noCameras ? <p className="muted">No cameras are shown in Playback. Choose all cameras, or add some to the view.</p> : null}
        </form>

        {found.error ? <p className="notice" data-tone="bad" role="alert">{failureText(found.error)}</p> : null}
        {data ? (
          <>
            <section className="attr-coverage" aria-label="Analysis coverage">
              <p className="notice" role="status" data-tone={data.coverage.fully_analysed ? "good" : "warn"}>
                {data.coverage.searched_cameras === 0 ? "No cameras were searched."
                  : data.coverage.fully_analysed ? `Every searched camera (${data.coverage.searched_cameras}) was analysed for the whole range.`
                  : `${data.coverage.analysed_percent}% of the searched time was analysed. A camera with no match in time that was not analysed was not looked at, not empty.`}
              </p>
              {data.coverage.cameras.length ? (
                <details>
                  <summary>Not analysed: {data.coverage.cameras.length} camera{data.coverage.cameras.length === 1 ? "" : "s"}</summary>
                  <ul className="plain-list attr-gaps" aria-label="Unanalysed time by camera">
                    {data.coverage.cameras.map(camera => (
                      <li key={camera.camera_id}>
                        <strong>{camera.camera_name}</strong>: {duration(camera.unanalysed_seconds)} not analysed
                        <ul>
                          {camera.intervals.map(item => (
                            <li key={`${item.state}-${item.starts_at}`}>{STATE_LABEL[item.state] ?? item.state}, {when(item.starts_at)} to {when(item.ends_at)} ({duration(item.seconds)}){item.detail && item.state !== "not_running" ? `: ${item.detail}` : ""}</li>
                          ))}
                          {camera.more_intervals ? <li>and {camera.more_intervals} more</li> : null}
                        </ul>
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </section>
            <p className="muted" role="status">{data.results.length ? `${data.results.length}${data.truncated ? "+" : ""} match${data.results.length === 1 ? "" : "es"}, newest first.${data.truncated ? " More matched than are shown: narrow the filters or the time." : ""}` : "No matches in the time that was analysed."}</p>
            <ul className="attr-grid" aria-label="Search results">
              {data.results.map(result => (
                <li key={result.track_id}>
                  <ResultCard result={result} when={when} onOpen={open} />
                  <button type="button" className="button attr-similar" data-variant="ghost" onClick={() => setSimilarTo(result)}
                          aria-label={`Find similar to ${result.camera_name} at ${when(result.first_seen_at)}: ${summary(result)}`}>Find similar</button>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        {similarTo ? (
          <section className="attr-similar-results" aria-label="Similar objects">
            <header className="attr-similar-head">
              <h3>Similar to {similarTo.camera_name}, {when(similarTo.first_seen_at)}</h3>
              <button type="button" className="button" data-variant="ghost" onClick={() => setSimilarTo(null)}>Close</button>
            </header>
            {alike.isFetching ? <p className="muted" role="status">Looking…</p> : null}
            {alike.error ? <p className="notice" data-tone="bad" role="alert">{failureText(alike.error)}</p> : null}
            {alike.data ? (
              <>
                <p className="muted" role="status">
                  {alike.data.results.length ? `${alike.data.results.length} similar object${alike.data.results.length === 1 ? "" : "s"} in the last 7 days, in time order. Similarity is how alike they look, not proof they are the same.` : "Nothing similar in the time that was analysed."}
                  {alike.data.coverage.fully_analysed ? "" : " Some cameras were not analysed for the whole time, so absence here is not proof."}
                </p>
                <ul className="attr-grid" aria-label="Similar results">
                  {alike.data.results.map(result => (
                    <li key={result.track_id}>
                      <ResultCard result={result} when={when} onOpen={open} similarity={result.similarity} />
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </section>
        ) : null}
      </div>
    </aside>
  );
}
