import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "@/api/client";
import { useCameraGroups, useDisplaySettings } from "@/api/queries";
import { failureText } from "@/features/admin/tabDraft";
import { formatSiteDateTime } from "@/lib/zonedTime";

interface Outcomes { new: number; acknowledged: number; assigned: number; resolved: number }
interface Body {
  period: { start: string; end: string };
  cameras: { id: string; name: string }[];
  alarms: {
    total: number; unresolved: number; truncated: boolean;
    by_rule: { rule_id: string | null; rule_name: string; total: number; outcomes: Outcomes }[];
    items: { id: string; rule_name: string; camera_name: string; label: string; state: string; occurred_at: string; link: string }[];
  };
  tracks: {
    after_hours_window: { start: string; end: string };
    after_hours_people: { total: number; items: { track_id: string; camera_name: string; first_seen_at: string; link: string }[] };
    vehicles_at_gates: { total: number; items: { event_id: string; rule_name: string; camera_name: string; occurred_at: string; link: string }[] };
  };
  gaps: { total_seconds: number; items: { camera_name: string; starts_at: string; seconds: number; reason: string; link: string }[] };
  incidents: { total: number; items: { id: string; camera_name: string | null; summary: string; state: string; first_seen_at: string; link: string }[] };
  analysis: { analysed_percent: number | null; fully_analysed: boolean; cameras: { camera_id: string; camera_name: string; unanalysed_seconds: number }[] };
}
interface Digest {
  id: string | null; name: string; period_start: string; period_end: string; headline: string; summary: string | null; summary_note: string | null; summary_error: string | null;
  digest: Body; text: string; created_at?: string;
}
interface Listed { id: string; name: string; period_start: string; period_end: string; headline: string }

const PERIODS = [{ id: "12", label: "Last 12 hours", hours: 12 }, { id: "24", label: "Last 24 hours", hours: 24 }, { id: "168", label: "Last 7 days", hours: 168 }];

function useWhen() {
  const display = useDisplaySettings();
  return (iso: string) => (display.data ? formatSiteDateTime(Date.parse(iso), display.data) : iso);
}

function minutes(seconds: number): string {
  const total = Math.round(seconds / 60);
  return total >= 60 ? `${Math.floor(total / 60)} h ${total % 60} min` : `${total} min`;
}

function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="digest-section" aria-label={title}>
      <h2>{title} <span className="muted">({count})</span></h2>
      {count ? children : <p className="muted">None in this period.</p>}
    </section>
  );
}

/** A digest's findings, each with its link to the clip or record. */
export function DigestBody({ digest }: { digest: Digest }) {
  const when = useWhen();
  const body = digest.digest;
  return (
    <article className="digest" aria-label={digest.name}>
      <p className="digest-headline" data-testid="digest-headline">{digest.headline}</p>
      <p className="muted">{when(digest.period_start)} to {when(digest.period_end)} · {body.cameras.map(camera => camera.name).join(", ") || "no cameras"}</p>
      {digest.summary ? (
        <section className="digest-summary" aria-label="Summary">
          <p>{digest.summary}</p>
          <p className="muted">{digest.summary_note}</p>
        </section>
      ) : null}
      {!body.analysis.fully_analysed ? (
        <p className="notice" data-tone="warn" role="status" data-testid="digest-unanalysed">
          Not all of this time was analysed{body.analysis.analysed_percent !== null ? ` (${body.analysis.analysed_percent}% was)` : ""}: a quiet stretch may mean nothing was looked at.
          {body.analysis.cameras.length ? ` ${body.analysis.cameras.map(item => `${item.camera_name}: ${minutes(item.unanalysed_seconds)} not analysed`).join("; ")}.` : ""}
        </p>
      ) : null}
      <Section title="Alarms" count={body.alarms.total}>
        <table className="insights-table" aria-label="Alarms by rule">
          <thead><tr><th>Rule</th><th className="num">Total</th><th className="num">New</th><th className="num">Acknowledged</th><th className="num">Assigned</th><th className="num">Resolved</th></tr></thead>
          <tbody>
            {body.alarms.by_rule.map(rule => (
              <tr key={`${rule.rule_id}:${rule.rule_name}`}><td>{rule.rule_name}</td><td className="num">{rule.total}</td><td className="num">{rule.outcomes.new}</td>
                <td className="num">{rule.outcomes.acknowledged}</td><td className="num">{rule.outcomes.assigned}</td><td className="num">{rule.outcomes.resolved}</td></tr>
            ))}
          </tbody>
        </table>
        <ul className="digest-list" aria-label="Alarms">
          {body.alarms.items.map(item => (
            <li key={item.id}><a href={item.link}>{when(item.occurred_at)} · {item.rule_name} on {item.camera_name}</a> <span className="muted">{item.label} · {item.state}</span></li>
          ))}
        </ul>
        {body.alarms.truncated ? <p className="muted">The first {body.alarms.items.length} are listed; the table counts all of them.</p> : null}
      </Section>
      <Section title={`People after hours (${body.tracks.after_hours_window.start} to ${body.tracks.after_hours_window.end})`} count={body.tracks.after_hours_people.total}>
        <ul className="digest-list" aria-label="People after hours">
          {body.tracks.after_hours_people.items.map(item => <li key={item.track_id}><a href={item.link}>{when(item.first_seen_at)} · {item.camera_name}</a></li>)}
        </ul>
      </Section>
      <Section title="Vehicles at gates" count={body.tracks.vehicles_at_gates.total}>
        <ul className="digest-list" aria-label="Vehicles at gates">
          {body.tracks.vehicles_at_gates.items.map(item => <li key={item.event_id}><a href={item.link}>{when(item.occurred_at)} · {item.rule_name} on {item.camera_name}</a></li>)}
        </ul>
      </Section>
      <Section title="Recording gaps" count={body.gaps.items.length}>
        <p className="muted">{minutes(body.gaps.total_seconds)} not recorded in all.</p>
        <ul className="digest-list" aria-label="Recording gaps">
          {body.gaps.items.map(item => <li key={`${item.camera_name}${item.starts_at}`}><a href={item.link}>{item.camera_name} · {when(item.starts_at)} for {minutes(item.seconds)}</a> <span className="muted">{item.reason}</span></li>)}
        </ul>
      </Section>
      <Section title="System incidents" count={body.incidents.total}>
        <ul className="digest-list" aria-label="System incidents">
          {body.incidents.items.map(item => <li key={item.id}><a href={item.link}>{when(item.first_seen_at)} · {item.summary}{item.camera_name ? ` (${item.camera_name})` : ""}</a> <span className="muted">{item.state}</span></li>)}
        </ul>
      </Section>
    </article>
  );
}

/** Digests: made on demand for a range, scope and your own view, and the ones scheduled for you. */
export function DigestsList() {
  const when = useWhen();
  const groups = useCameraGroups();
  const [period, setPeriod] = useState("24");
  const [group, setGroup] = useState("");
  const [asked, setAsked] = useState<{ start: string; end: string; group: string } | null>(null);
  const saved = useQuery({ queryKey: ["digests"], queryFn: ({ signal }) => api<{ digests: Listed[] }>("/api/digests", { signal }).then(result => result.digests) });
  const made = useQuery({
    queryKey: ["digest-preview", asked],
    enabled: asked !== null,
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ start: asked!.start, end: asked!.end });
      if (asked!.group) params.append("group_ids", asked!.group);
      else params.set("site", "true");
      return api<Digest>(`/api/digests/preview?${params}`, { signal });
    },
  });

  function make() {
    const hours = PERIODS.find(item => item.id === period)?.hours ?? 24;
    const end = new Date();
    setAsked({ start: new Date(end.getTime() - hours * 3_600_000).toISOString(), end: end.toISOString(), group });
  }

  return (
    <section className="cases" aria-labelledby="digests-title">
      <header className="cases-head"><h1 id="digests-title">Digests</h1></header>
      <form className="insights-controls" aria-label="Make a digest" onSubmit={event => { event.preventDefault(); make(); }}>
        <label className="field"><span>Period</span>
          <select value={period} onChange={event => setPeriod(event.target.value)}>{PERIODS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
        </label>
        <label className="field"><span>Cover</span>
          <select value={group} onChange={event => setGroup(event.target.value)}>
            <option value="">The whole site</option>
            {(groups.data ?? []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <button type="submit" className="button" data-variant="primary">Make digest</button>
      </form>
      {made.error ? <p className="notice" data-tone="bad" role="alert">{failureText(made.error, "The digest could not be made.")}</p> : null}
      {made.isFetching ? <p className="muted">Writing the digest…</p> : null}
      {made.data && !made.isFetching ? <DigestBody digest={made.data} /> : null}
      <h2>Scheduled for you</h2>
      {saved.isPending ? <p className="muted">Loading…</p> : null}
      {saved.data && !saved.data.length ? <p className="muted">No scheduled digests yet. An administrator sets them up in Settings.</p> : null}
      <ul className="status-list cases-list" aria-label="Scheduled digests">
        {(saved.data ?? []).map(item => (
          <li key={item.id}>
            <Link className="row-select case-row" to={`/digests/${item.id}`}><span>{item.name}</span><span className="muted">{when(item.period_start)} to {when(item.period_end)}</span><span className="muted">{item.headline}</span></Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function DigestView() {
  const { digestId } = useParams();
  const digest = useQuery({ queryKey: ["digest", digestId], queryFn: ({ signal }) => api<Digest>(`/api/digests/${digestId}`, { signal }) });
  return (
    <section className="cases" aria-labelledby="digest-title">
      <header className="cases-head">
        <h1 id="digest-title">{digest.data?.name ?? "Digest"}</h1>
        <Link className="button" data-variant="ghost" to="/digests">All digests</Link>
      </header>
      {digest.error ? <p className="notice" data-tone="bad" role="alert">{failureText(digest.error, "This digest could not be loaded.")}</p> : null}
      {digest.isPending ? <p className="muted">Loading…</p> : null}
      {digest.data ? <DigestBody digest={digest.data} /> : null}
    </section>
  );
}
