import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/api/client";
import type { Camera } from "@/api/types";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";

type Role = "administrator" | "operator";
interface TalkSettings { enabled: boolean; roles: Role[]; record_audio: boolean }
interface TalkSession { id: string; username: string; started_at: string; seconds: number | null; ended_reason: string | null; has_audio: boolean }

const ROLE_LABEL: Record<Role, string> = { administrator: "Administrators", operator: "Operators" };
const DEFAULTS: TalkSettings = { enabled: false, roles: ["administrator"], record_audio: false };
const REASON: Record<string, string> = {
  released: "Let go", idle: "Went quiet", limit: "Reached the time limit", unsupported: "Camera has no usable speaker", camera_failed: "Camera stopped answering", bad_audio: "Bad audio",
};

/** Whether people can talk through this camera's speaker from Live, which roles may, whether the audio is kept, and the camera's talk history. */
export function TalkTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const base = `/api/cameras/${encodeURIComponent(camera.id)}/talk`;
  const queryKey = ["cameras", camera.id, "talk"] as const;
  const talk = useQuery({ queryKey, queryFn: ({ signal }) => api<TalkSettings>(base, { signal }) });
  const sessions = useQuery({ queryKey: [...queryKey, "sessions"], queryFn: ({ signal }) => api<{ sessions: TalkSession[] }>(`${base}/sessions`, { signal }), enabled: admin });
  const current = useMemo(() => (talk.data ? { ...DEFAULTS, ...talk.data } : undefined), [talk.data]);
  const tab = useTabDraft(current, {
    label: `Talk settings of ${camera.name}`,
    editable: admin,
    save: async (draft, saved) => {
      try {
        const next = await api<TalkSettings>(base, { method: "PUT", body: { enabled: draft.enabled, roles: draft.roles, record_audio: draft.record_audio } });
        client.setQueryData(queryKey, next);
        void client.invalidateQueries({ queryKey: ["talk"] });
        return { ok: true, base: { ...DEFAULTS, ...next }, outcome: { tone: "good", text: next.enabled ? "Saved. Talk is on for this camera." : "Saved. Talk is off for this camera." } };
      } catch (error) {
        return { ok: false, base: saved, outcome: { tone: "bad", text: `Talk settings not saved: ${failureText(error)}.` } };
      }
    },
  });
  if (talk.isPending) return <p className="muted">Loading the talk settings…</p>;
  if (talk.error || !tab.draft) return <p className="notice" data-tone="bad" role="alert">{failureText(talk.error, "The talk settings could not be loaded.")}</p>;
  const draft = tab.draft;
  const toggleRole = (role: Role, on: boolean) => tab.change({ roles: (["administrator", "operator"] as Role[]).filter(item => (item === role ? on : draft.roles.includes(item))) });
  const invalid = draft.enabled && draft.roles.length === 0 ? "Choose at least one role that may talk." : null;
  return (
    <form className="tab-form" aria-label="Talk" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="muted field-hint">
        Hold the Talk button on this camera's Live tile (or the T key) and the microphone is sent to the camera's speaker over the ONVIF audio backchannel. One person talks at a time and
        the tile shows who. Every talk is in the audit log. The camera's speaker must accept G.711 audio; the first talk tells you if it does not.
      </p>
      <fieldset className="field-group" disabled={!admin}>
        <legend>Talk</legend>
        <label className="field field-check"><input type="checkbox" checked={draft.enabled} onChange={event => tab.change({ enabled: event.target.checked })} /><span>Allow talking through this camera</span></label>
        {draft.enabled ? (
          <>
            <fieldset className="field-group" aria-label="Who may talk">
              <legend>Who may talk</legend>
              {(Object.keys(ROLE_LABEL) as Role[]).map(role => (
                <label key={role} className="field field-check"><input type="checkbox" checked={draft.roles.includes(role)} onChange={event => toggleRole(role, event.target.checked)} /><span>{ROLE_LABEL[role]}</span></label>
              ))}
            </fieldset>
            <label className="field field-check"><input type="checkbox" checked={draft.record_audio} onChange={event => tab.change({ record_audio: event.target.checked })} /><span>Keep the audio of each talk (30 days)</span></label>
            <p className="muted field-hint">Off by default: only who talked, when and for how long is kept.</p>
          </>
        ) : null}
      </fieldset>
      {invalid ? <p className="muted field-hint">{invalid}</p> : null}
      {admin ? <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert} blocked={invalid} /> : null}
      {admin ? (
        <section aria-label="Talk history">
          <h3>Recent talks</h3>
          {sessions.data?.sessions.length ? (
            <ul className="plain-list">
              {sessions.data.sessions.map(session => (
                <li key={session.id}>
                  {session.username}, {new Date(session.started_at).toLocaleString()}, {session.seconds === null ? "in progress" : `${Math.round(session.seconds)} s`}
                  {session.ended_reason ? ` (${REASON[session.ended_reason] ?? session.ended_reason})` : ""}
                  {session.has_audio ? <> · <a href={`${base}/sessions/${session.id}/audio`} download>Audio</a></> : null}
                </li>
              ))}
            </ul>
          ) : <p className="muted">Nobody has talked through this camera yet.</p>}
        </section>
      ) : null}
    </form>
  );
}
