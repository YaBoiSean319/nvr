import { useEffect } from "react";
import { useNavigate } from "react-router";
import { create } from "zustand";
import { ApiError } from "@/api/client";
import { useAcknowledgeIncident, useCameras, useConsoleStatus, useExportJobs, useFailoverStatus, useOpenIncidents, type ConsoleSite, type FailoverStatus } from "@/api/queries";
import type { Camera } from "@/api/types";
import { Drawer } from "@/components/Drawer";
import { AlarmsInbox } from "@/features/alarms/AlarmsInbox";
import { useAlarms } from "@/features/alarms/api";
import { ExportJobs } from "@/features/exports/ExportJobs";
import { usePlaybackStore } from "@/features/playback/store";
import { formatRelative } from "@/lib/time";
import { summarize, type StatusSummary, type Tone } from "./summary";

type Section = "video" | "recording" | "incidents" | "jobs" | "sites" | "failover";

/** Which part of the status drawer is open; other workspaces can open it (for example, export jobs). */
export const useStatusPanel = create<{ section: Section | null; show: (section: Section | null) => void }>(set => ({
  section: null,
  show: section => set({ section }),
}));

/** Whether the alarm inbox is open, and on which alarm (for example one opened from a notification). */
export const useAlarmInbox = create<{ open: boolean; alarmId: string | null; setOpen: (open: boolean) => void; openAlarm: (id: string) => void }>(set => ({
  open: false,
  alarmId: null,
  setOpen: open => set(open ? { open } : { open, alarmId: null }),
  openAlarm: id => set({ open: true, alarmId: id }),
}));

/**
 * A notification or digest opens the app at /?alarm=<id> (show that alarm), /playback?playback=<camera>&at=<time> (open Playback there) or
 * /?incident=<id> (open the incidents): do it, and tidy the address.
 */
function useLinkFromAddress() {
  const navigate = useNavigate();
  useEffect(() => {
    const url = new URL(window.location.href);
    const alarm = url.searchParams.get("alarm");
    const camera = url.searchParams.get("playback");
    const at = Date.parse(url.searchParams.get("at") ?? "");
    const incident = url.searchParams.get("incident");
    if (!alarm && !camera && !incident) return;
    if (alarm) useAlarmInbox.getState().openAlarm(alarm);
    if (camera) {
      usePlaybackStore.getState().openInPlayback(camera, Number.isNaN(at) ? undefined : at);
      navigate("/playback");
    }
    if (incident) useStatusPanel.getState().show("incidents");
    for (const key of ["alarm", "playback", "at", "incident"]) url.searchParams.delete(key);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, [navigate]);
}

function failoverTone(status: FailoverStatus): Tone {
  if (status.role === "standby") return status.takeover_active ? "bad" : status.last_sync_error ? "warn" : "good";
  return status.peer_state === "ok" ? "good" : status.peer_state === "unreachable" || status.peer_state === "refused" || status.peer_state === "error" ? "warn" : "warn";
}

function failoverLabel(status: FailoverStatus): string {
  if (status.role === "standby") return status.takeover_active ? "Failover: standby recording" : "Failover: standby";
  return status.peer_state === "ok" ? "Failover: standby watching" : status.peer_state === "handback" ? "Failover: bringing footage back" : "Failover: check standby";
}

function Chip({ tone, label, onClick }: { tone: Tone; label: string; onClick: () => void }) {
  return (
    <button type="button" className="status-chip" onClick={onClick}>
      <span className="status-dot" data-tone={tone} aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}

/** Compact operational status that stays available in every workspace. */
export function StatusArea() {
  const cameras = useCameras();
  const incidents = useOpenIncidents();
  const jobs = useExportJobs();
  const consoleStatus = useConsoleStatus();
  const failover = useFailoverStatus().data;
  const sites = consoleStatus.data?.enabled ? consoleStatus.data.sites : [];
  const downSites = sites.filter(site => site.state === "unreachable" || site.state === "refused");
  const section = useStatusPanel(state => state.section);
  const setSection = useStatusPanel(state => state.show);
  const summary = summarize(cameras.data, incidents.data, jobs.data);
  const unreachable = cameras.isError || incidents.isError;
  const alarms = useAlarms({ state: "open", camera_id: "", rule_id: "" });
  const inbox = useAlarmInbox();
  useLinkFromAddress();
  const counts = alarms.data?.counts;
  const openAlarms = counts ? counts.new + counts.acknowledged + counts.assigned : 0;
  const alarmLabel = !counts ? "Alarms" : counts.new ? `${counts.new} new alarm${counts.new === 1 ? "" : "s"}` : openAlarms ? `${openAlarms} open alarm${openAlarms === 1 ? "" : "s"}` : "No open alarms";
  return (
    <>
      <div className="status-area" role="status" aria-label="Operational status">
        {unreachable ? <Chip tone="bad" label="Status unavailable" onClick={() => setSection("incidents")} /> : null}
        <Chip tone={summary.recording.tone} label={summary.recording.label} onClick={() => setSection("recording")} />
        <Chip tone={summary.video.tone} label={summary.video.label} onClick={() => setSection("video")} />
        <Chip tone={summary.incidents.tone} label={summary.incidents.label} onClick={() => setSection("incidents")} />
        <Chip tone={summary.jobs.tone} label={summary.jobs.label} onClick={() => setSection("jobs")} />
        {sites.length ? <Chip tone={downSites.length ? "bad" : "good"} label={downSites.length ? `Sites: ${downSites.length} unreachable` : `Sites: ${sites.length} reachable`} onClick={() => setSection("sites")} /> : null}
        {failover && failover.role !== "off" ? <Chip tone={failoverTone(failover)} label={failoverLabel(failover)} onClick={() => setSection("failover")} /> : null}
        <Chip tone={counts?.new ? "bad" : openAlarms ? "warn" : "good"} label={alarmLabel} onClick={() => inbox.setOpen(true)} />
      </div>
      <Drawer open={inbox.open} onOpenChange={inbox.setOpen} title="Alarms"
              description="What rules saw. Acknowledge, assign, comment and resolve; everyone signed in sees changes within seconds.">
        <AlarmsInbox key={inbox.alarmId ?? "inbox"} initialAlarmId={inbox.alarmId} onNavigate={() => inbox.setOpen(false)} />
      </Drawer>
      <Drawer open={section !== null} onOpenChange={open => !open && setSection(null)} title="Operational status"
              description="Live video health and recording health are reported separately; a live picture does not prove footage is being written.">
        <StatusDetails summary={summary} focus={section} sites={sites} failover={failover} />
      </Drawer>
    </>
  );
}

function CameraList({ cameras, empty }: { cameras: Camera[]; empty: string }) {
  if (!cameras.length) return <p className="muted">{empty}</p>;
  return (
    <ul className="status-list">
      {cameras.map(camera => (
        <li key={camera.id}>
          <strong>{camera.name}</strong>
          <span className="muted">{camera.recording_health.detail}</span>
        </li>
      ))}
    </ul>
  );
}

function StatusDetails({ summary, focus, sites, failover }: { summary: StatusSummary; focus: Section | null; sites: ConsoleSite[]; failover: FailoverStatus | undefined }) {
  const acknowledge = useAcknowledgeIncident();
  return (
    <div className="status-details">
      <section aria-labelledby="status-recording" data-focused={focus === "recording"}>
        <h3 id="status-recording">Recording</h3>
        <CameraList cameras={[...summary.recording.failing, ...summary.recording.uncertain]} empty="Every enabled camera is recording as its policy expects." />
      </section>
      <section aria-labelledby="status-video" data-focused={focus === "video"}>
        <h3 id="status-video">Live video</h3>
        <CameraList cameras={summary.video.cameras} empty="Every recorded camera has delivered fresh video recently. Freshness is measured for cameras with recording enabled." />
      </section>
      <section aria-labelledby="status-incidents" data-focused={focus === "incidents"}>
        <h3 id="status-incidents">Incidents</h3>
        {acknowledge.error ? <p className="notice" data-tone="bad" role="alert">{acknowledge.error instanceof ApiError ? acknowledge.error.detail : "Could not acknowledge the incident."}</p> : null}
        {summary.incidents.open.length ? (
          <ul className="status-list">
            {summary.incidents.open.map(incident => (
              <li key={incident.id}>
                <strong>{incident.summary}</strong>
                <span className="muted">
                  {incident.state === "acknowledged" ? "Acknowledged, still active" : "Active"} · last seen {formatRelative(incident.last_seen_at)}
                  {incident.recurrence_count > 1 ? ` · occurred ${incident.recurrence_count} times` : ""}
                </span>
                {incident.state === "active" ? (
                  <button type="button" className="button" onClick={() => acknowledge.mutate(incident.id)} disabled={acknowledge.isPending}>
                    Acknowledge
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No active incidents.</p>
        )}
      </section>
      {sites.length ? (
        <section aria-labelledby="status-sites" data-focused={focus === "sites"}>
          <h3 id="status-sites">Sites</h3>
          <ul className="status-list">
            {sites.map(site => (
              <li key={site.id}>
                <strong>{site.name}</strong>
                <span className="muted">
                  {site.state === "ok" ? "Reachable" : site.state === "refused" ? "Refused the console's token" : site.state === "disabled" ? "Off" : "Unreachable"}
                  {" · last contact "}{site.last_contact_at ? formatRelative(site.last_contact_at) : "never"}
                  {site.camera_count !== null ? ` · ${site.camera_count} cameras` : ""}{site.open_alarms ? ` · ${site.open_alarms} open alarms` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {failover && failover.role !== "off" ? (
        <section aria-labelledby="status-failover" data-focused={focus === "failover"}>
          <h3 id="status-failover">Failover</h3>
          <p>{failover.headline}</p>
          <p className="muted">
            {failover.role === "standby" ? `Last heard from the primary ${failover.last_contact_at ? formatRelative(failover.last_contact_at) : "never"}` : `Last answer from the standby ${failover.last_contact_at ? formatRelative(failover.last_contact_at) : "never"}`}
            {failover.role === "standby" ? ` · configuration copied ${failover.last_sync_at ? formatRelative(failover.last_sync_at) : "never"}` : ""}
            {failover.last_error ? ` · ${failover.last_error}` : ""}{failover.last_sync_error ? ` · ${failover.last_sync_error}` : ""}
          </p>
        </section>
      ) : null}
      <section aria-labelledby="status-jobs" data-focused={focus === "jobs"}>
        <h3 id="status-jobs">Export jobs</h3>
        <ExportJobs />
      </section>
    </div>
  );
}
