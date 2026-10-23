import { useState } from "react";
import { api } from "@/api/client";
import type { CameraBrand, OnvifProfile } from "@/api/types";
import { failureText } from "../../tabDraft";
import { BRAND_LABEL } from "../inventory";

/** Connection details for a camera that is not added yet. Secrets live only in this draft. */
export interface Connection {
  mode: "onvif" | "url";
  address: string;
  port: string;
  endpoint: string;
  stream_url: string;
  username: string;
  password: string;
  brand: CameraBrand;
  profile: string;
  substream: string;
}

export const EMPTY_CONNECTION: Connection = {
  mode: "onvif", address: "", port: "80", endpoint: "", stream_url: "", username: "", password: "", brand: "auto", profile: "", substream: "",
};

export function deviceServiceUrl(connection: Connection): string | null {
  if (connection.endpoint.trim()) return connection.endpoint.trim();
  const port = Number(connection.port);
  if (!connection.address.trim() || !Number.isInteger(port) || port < 1 || port > 65535) return null;
  return `http://${connection.address.trim()}:${port}/onvif/device_service`;
}

/** The request body shared by testing, adding and linking. */
export function sourceBody(connection: Connection) {
  const value = (text: string) => text.trim() || null;
  return connection.mode === "onvif"
    ? {
        camera_ip: value(connection.address), stream_url: null,
        camera_username: value(connection.username), camera_password: connection.password || null, camera_brand: connection.brand,
        device_service_url: deviceServiceUrl(connection), onvif_profile_token: value(connection.profile),
        live_substream_profile_token: value(connection.substream),
      }
    : {
        camera_ip: null, stream_url: value(connection.stream_url),
        camera_username: value(connection.username), camera_password: connection.password || null, camera_brand: connection.brand,
        device_service_url: null, onvif_profile_token: null, live_substream_profile_token: null,
      };
}

export function ConnectionFields({ value, onChange, allowUrl = true }: { value: Connection; onChange: (next: Connection) => void; allowUrl?: boolean }) {
  const [profiles, setProfiles] = useState<{ busy: boolean; list?: OnvifProfile[]; error?: string }>({ busy: false });
  const set = (change: Partial<Connection>) => onChange({ ...value, ...change });

  async function loadProfiles() {
    setProfiles({ busy: true });
    try {
      const body = sourceBody(value);
      const result = await api<{ device_service_url: string | null; profiles: OnvifProfile[] }>("/api/cameras/onvif-profiles", {
        method: "POST",
        body: { camera_ip: body.camera_ip, camera_username: body.camera_username, camera_password: body.camera_password, camera_brand: body.camera_brand, device_service_url: body.device_service_url },
      });
      setProfiles({ busy: false, list: result.profiles });
      onChange({ ...value, endpoint: result.device_service_url ?? value.endpoint, profile: value.profile || result.profiles[0]?.token || "" });
    } catch (error) {
      setProfiles({ busy: false, error: failureText(error, "Profiles could not be loaded.") });
    }
  }

  const list = profiles.list ?? [];
  return (
    <>
      {allowUrl ? (
        <div className="segmented" role="radiogroup" aria-label="Connect by">
          <label><input type="radio" checked={value.mode === "onvif"} onChange={() => set({ mode: "onvif" })} /> Camera address (ONVIF)</label>
          <label><input type="radio" checked={value.mode === "url"} onChange={() => set({ mode: "url" })} /> Direct RTSP URL</label>
        </div>
      ) : null}
      {value.mode === "onvif" ? (
        <>
          <div className="field-row">
            <label className="field"><span>IP address</span><input className="mono" value={value.address} autoComplete="off" onChange={event => set({ address: event.target.value })} /></label>
            <label className="field"><span>ONVIF port</span><input type="number" min={1} max={65535} value={value.port} onChange={event => set({ port: event.target.value })} /></label>
          </div>
          <label className="field"><span>ONVIF device service (optional)</span>
            <input className="mono" value={value.endpoint} placeholder={deviceServiceUrl({ ...value, endpoint: "" }) ?? "Built from the address and port"} autoComplete="off" onChange={event => set({ endpoint: event.target.value })} />
          </label>
        </>
      ) : (
        <label className="field"><span>RTSP URL</span><input className="mono" value={value.stream_url} placeholder="rtsp://camera.example/stream" autoComplete="off" onChange={event => set({ stream_url: event.target.value })} /></label>
      )}
      <div className="field-row">
        <label className="field"><span>Username</span><input value={value.username} autoComplete="off" onChange={event => set({ username: event.target.value })} /></label>
        <label className="field"><span>Password</span><input type="password" value={value.password} autoComplete="new-password" onChange={event => set({ password: event.target.value })} /></label>
      </div>
      <label className="field"><span>Brand</span>
        <select value={value.brand} onChange={event => set({ brand: event.target.value as CameraBrand })}>
          {(Object.keys(BRAND_LABEL) as CameraBrand[]).map(brand => <option key={brand} value={brand}>{brand === "auto" ? "Detect automatically" : BRAND_LABEL[brand]}</option>)}
        </select>
      </label>
      {value.mode === "onvif" ? (
        <>
          <div className="field-actions">
            <button type="button" className="button" onClick={() => void loadProfiles()} disabled={profiles.busy || !value.address.trim()}>{profiles.busy ? "Loading profiles…" : "Load stream profiles"}</button>
            {profiles.error ? <span className="field-error" role="alert">{profiles.error}</span> : null}
          </div>
          <div className="field-row">
            <label className="field"><span>Main (recording) profile</span>
              <select value={value.profile} onChange={event => set({ profile: event.target.value })}>
                <option value="">Camera default</option>
                {list.map(profile => <option key={profile.token} value={profile.token}>{profile.name || profile.token}</option>)}
              </select>
            </label>
            <label className="field"><span>Live substream (optional)</span>
              <select value={value.substream} onChange={event => set({ substream: event.target.value })}>
                <option value="">None</option>
                {list.filter(profile => profile.token !== value.profile).map(profile => <option key={profile.token} value={profile.token}>{profile.name || profile.token}</option>)}
              </select>
            </label>
          </div>
        </>
      ) : null}
    </>
  );
}
