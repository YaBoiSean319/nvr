import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "@/api/client";
import type { DisplaySettings } from "@/api/types";
import { failureText } from "@/features/admin/tabDraft";
import type { PlateSearchResponse } from "@/features/plates/api";
import { formatSiteDateTime } from "@/lib/zonedTime";
import { usePlaybackStore } from "./store";

const WHEN: Record<string, { label: string; hours: number }> = {
  "24h": { label: "Last 24 hours", hours: 24 }, "7d": { label: "Last 7 days", hours: 168 }, "30d": { label: "Last 30 days", hours: 720 }, "90d": { label: "Last 90 days", hours: 2160 },
};

/** Find a licence plate, in full or in part (* for any run of characters, ? for one), on the cameras this person may see; each read opens Playback at that moment. */
export function PlateSearch({ settings, shownIds }: { settings: DisplaySettings; shownIds: string[] }) {
  const [text, setText] = useState("");
  const [when, setWhen] = useState("7d");
  const [cameras, setCameras] = useState<"all" | "view">("all");
  const [lookalikes, setLookalikes] = useState(true);
  const [submitted, setSubmitted] = useState<{ text: string; when: string; cameras: "all" | "view"; lookalikes: boolean; sequence: number } | null>(null);
  const shown = (iso: string) => formatSiteDateTime(Date.parse(iso), settings);

  const found = useQuery({
    queryKey: ["plate-search", submitted, submitted?.cameras === "view" ? shownIds : null],
    enabled: submitted !== null,
    staleTime: 0,
    retry: false,
    queryFn: ({ signal }) => {
      const query = submitted!;
      const end = Date.now();
      const params = new URLSearchParams({ q: query.text, start: new Date(end - (WHEN[query.when]?.hours ?? 168) * 3_600_000).toISOString(), end: new Date(end).toISOString(), lookalikes: String(query.lookalikes) });
      if (query.cameras === "view") shownIds.forEach(id => params.append("camera_ids", id));
      return api<PlateSearchResponse>(`/api/plates/search?${params}`, { signal });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (text.trim()) setSubmitted(current => ({ text: text.trim(), when, cameras, lookalikes, sequence: (current?.sequence ?? 0) + 1 }));
  }
  const noCameras = cameras === "view" && shownIds.length === 0;
  const data = found.data;
  return (
    <aside className="export-drawer" id="plate-search" aria-label="Plate search">
      <header className="export-head">
        <h2>Find a plate</h2>
        <button type="button" className="button icon-button" data-variant="ghost" aria-label="Close plate search" onClick={() => usePlaybackStore.getState().setPlateSearchOpen(false)}>✕</button>
      </header>
      <div className="export-body attr-body">
        <form className="attr-form" onSubmit={submit} role="search" aria-label="Find a plate" noValidate>
          <label className="attr-field"><span>Plate</span>
            <input type="search" value={text} maxLength={16} placeholder="AB12CDE, 12CD or AB1*" onChange={event => setText(event.target.value)} />
            <span className="muted">Part of a plate finds plates containing it. Use * for any run of characters and ? for one character; then the whole plate must match (AB12* starts with, *CDE ends with).</span>
          </label>
          <label className="attr-field"><span>Cameras</span>
            <select value={cameras} onChange={event => setCameras(event.target.value as "all" | "view")}>
              <option value="all">All cameras I can see</option><option value="view">Cameras in this Playback view</option>
            </select>
          </label>
          <label className="attr-field"><span>When</span>
            <select value={when} onChange={event => setWhen(event.target.value)}>{Object.entries(WHEN).map(([key, item]) => <option key={key} value={key}>{item.label}</option>)}</select>
          </label>
          <label className="check"><input type="checkbox" checked={lookalikes} onChange={event => setLookalikes(event.target.checked)} /> Also find look-alike characters (O, Q or D and 0, I or L and 1, S and 5, B and 8, Z and 2)</label>
          <div className="attr-actions"><button type="submit" className="button" data-variant="primary" disabled={!text.trim() || noCameras || found.isFetching}>{found.isFetching ? "Searching…" : "Search plates"}</button></div>
          {noCameras ? <p className="muted">No cameras are shown in Playback. Choose all cameras, or add some to the view.</p> : null}
        </form>
        {found.error ? <p className="notice" data-tone="bad" role="alert">{failureText(found.error)}</p> : null}
        {data ? (
          <>
            <p className="muted" role="status">
              {data.results.length ? `${data.results.length}${data.truncated ? "+" : ""} read${data.results.length === 1 ? "" : "s"}, newest first.${data.truncated ? " More matched than are shown: narrow the plate or the time." : ""}` : "No plate read matched."}
              {data.cameras_reading.length === 0 ? " None of the cameras searched is set to read plates (an administrator turns that on under Settings, Licence plates)." : ""}
            </p>
            <p className="muted">{data.note}</p>
            <ul className="attr-grid" aria-label="Plate results">
              {data.results.map(read => (
                <li key={read.id}>
                  <button type="button" className="attr-card" onClick={() => usePlaybackStore.getState().openInPlayback(read.camera_id, Date.parse(read.read_at))}
                          aria-label={`Open ${read.camera_name} in Playback at ${shown(read.read_at)}: plate ${read.plate}, ${Math.round(read.confidence * 100)}% confident`}>
                    <span className="attr-thumb">{read.has_crop ? <img src={`/api/plates/reads/${read.id}/crop`} alt="" loading="lazy" /> : <span className="attr-nopicture">No picture</span>}</span>
                    <span className="attr-meta"><strong>{read.plate}</strong><span>{read.camera_name}</span><span>{shown(read.read_at)}</span><span className="attr-score">{Math.round(read.confidence * 100)}% confident</span></span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </aside>
  );
}
