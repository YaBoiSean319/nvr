import { useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { keys } from "@/api/queries";
import type { Camera } from "@/api/types";
import { TabFooter } from "../TabFooter";
import { failureText, useTabDraft } from "../tabDraft";
import { configureMotion, type Zone } from "./api";
import { ZoneEditor } from "./ZoneEditor";

interface Draft {
  /** "" keeps the saved event service; the saved URL is not shown back. */
  service_url: string;
  remove_service: boolean;
  username: string;
  password: string;
  fallback_enabled: boolean;
  sensitivity: number;
  zones: Zone[];
}

const published = (camera: Camera): Draft => ({
  service_url: "", remove_service: false, username: "", password: "",
  fallback_enabled: camera.motion_fallback_enabled, sensitivity: camera.motion_sensitivity, zones: camera.motion_zones,
});

const STATUS_LABEL: Record<string, string> = {
  active: "Receiving ONVIF motion events", untested: "Configured, not yet confirmed", unsupported: "No ONVIF event service", degraded: "Event service degraded",
};

/** ONVIF event source, server fallback, sensitivity and motion zones. */
export function MotionTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const current = useMemo(() => published(camera), [camera]);
  const tab = useTabDraft(current, {
    label: `Motion for ${camera.name}`,
    editable: admin,
    save: async draft => {
      try {
        const updated = await configureMotion(camera.id, {
          ...(draft.remove_service ? { service_url: null } : draft.service_url.trim() ? { service_url: draft.service_url.trim() } : {}),
          ...(draft.username || draft.password ? { username: draft.username, password: draft.password } : {}),
          fallback_enabled: draft.fallback_enabled, sensitivity: draft.sensitivity, zones: draft.zones,
        });
        void client.invalidateQueries({ queryKey: keys.cameras });
        return { ok: true, base: published(updated), outcome: { tone: "good", text: "Saved motion settings." } };
      } catch (error) {
        return { ok: false, base: current, outcome: { tone: "bad", text: `Motion settings not saved: ${failureText(error)}.` } };
      }
    },
  });
  const draft = tab.draft!;
  const readOnly = !admin;
  return (
    <form className="tab-form" aria-label="Motion settings" noValidate onSubmit={event => { event.preventDefault(); void tab.save(); }}>
      <p className="notice">
        ONVIF events: {STATUS_LABEL[camera.motion_source_status] ?? camera.motion_source_status}. Server motion: {camera.server_motion_status}.
      </p>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>ONVIF event service</legend>
        <label className="field"><span>Event service URL</span>
          <input className="mono" value={draft.service_url} disabled={draft.remove_service} autoComplete="off"
                 placeholder={camera.motion_source_configured ? "Configured (enter a URL to replace it)" : "Not configured"}
                 onChange={event => tab.change({ service_url: event.target.value })} />
        </label>
        {camera.motion_source_configured ? (
          <label className="check"><input type="checkbox" checked={draft.remove_service}
                 onChange={event => tab.change({ remove_service: event.target.checked, service_url: "" })} /> Remove the event service</label>
        ) : null}
        <div className="field-row">
          <label className="field"><span>Username</span><input value={draft.username} autoComplete="off" onChange={event => tab.change({ username: event.target.value })} /></label>
          <label className="field"><span>Password</span><input type="password" value={draft.password} autoComplete="new-password" onChange={event => tab.change({ password: event.target.value })} /></label>
        </div>
        <p className="muted field-hint">Saved event credentials are never shown. Leave both blank to keep them.</p>
      </fieldset>
      <fieldset className="field-group" disabled={readOnly}>
        <legend>Server motion detection</legend>
        <label className="check"><input type="checkbox" checked={draft.fallback_enabled} onChange={event => tab.change({ fallback_enabled: event.target.checked })} /> Detect motion on the server when ONVIF events are unavailable</label>
        <label className="field"><span>Sensitivity: {draft.sensitivity}</span>
          <input type="range" min={1} max={100} value={draft.sensitivity} aria-label="Sensitivity" onChange={event => tab.change({ sensitivity: Number(event.target.value) })} />
        </label>
      </fieldset>
      <ZoneEditor cameraId={camera.id} zones={draft.zones} onChange={zones => tab.change({ zones })} label="Server motion zones" readOnly={readOnly} />
      {admin ? (
        <TabFooter outcome={tab.outcome} dirty={tab.dirty} saving={tab.saving} onRevert={tab.revert}
                   blocked={Boolean(draft.username) !== Boolean(draft.password) ? "Enter both the event username and password, or leave both blank." : null} />
      ) : null}
    </form>
  );
}
