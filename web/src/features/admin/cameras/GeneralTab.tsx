import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { keys } from "@/api/queries";
import type { Camera, CameraBrand, OnvifProfile, StreamProbe } from "@/api/types";
import { useDraft } from "../drafts";
import { loadProfiles, renameCamera, testSource, updateSource, type SourceDraft } from "./api";
import { BRAND_LABEL } from "./inventory";

interface Draft {
  name: string;
  mode: "onvif" | "url";
  camera_ip: string;
  stream_url: string;
  username: string;
  password: string;
  clear_credentials: boolean;
  camera_brand: CameraBrand;
  device_service_url: string;
  onvif_profile_token: string;
  live_substream_profile_token: string;
}

function published(camera: Camera): Draft {
  return {
    name: camera.name,
    mode: camera.camera_ip ? "onvif" : "url",
    camera_ip: camera.camera_ip ?? "",
    // Saved URLs come back without credentials or path; changing a direct URL means entering it again.
    stream_url: "",
    username: "",
    password: "",
    clear_credentials: false,
    camera_brand: camera.camera_brand ?? "auto",
    device_service_url: camera.device_service_url ?? "",
    onvif_profile_token: camera.onvif_profile_token ?? "",
    live_substream_profile_token: camera.live_substream_profile_token ?? "",
  };
}

const SOURCE_FIELDS: (keyof Draft)[] = ["mode", "camera_ip", "stream_url", "username", "password", "clear_credentials", "camera_brand", "device_service_url", "onvif_profile_token", "live_substream_profile_token"];

function sourceOf(draft: Draft): SourceDraft {
  const value = (text: string) => text.trim() || null;
  return {
    camera_ip: draft.mode === "onvif" ? value(draft.camera_ip) : null,
    stream_url: draft.mode === "url" ? value(draft.stream_url) : null,
    camera_username: value(draft.username),
    camera_password: draft.password || null,
    camera_brand: draft.camera_brand,
    device_service_url: draft.mode === "onvif" ? value(draft.device_service_url) : null,
    onvif_profile_token: draft.mode === "onvif" ? value(draft.onvif_profile_token) : null,
    live_substream_profile_token: draft.mode === "onvif" ? value(draft.live_substream_profile_token) : null,
    clear_credentials: draft.clear_credentials,
  };
}

const detail = (error: unknown, fallback: string) => (error instanceof ApiError ? error.detail : fallback);

/** Name, address, credentials, brand and ONVIF profiles, saved explicitly. */
export function GeneralTab({ camera, admin }: { camera: Camera; admin: boolean }) {
  const client = useQueryClient();
  const [base, setBase] = useState(() => published(camera));
  const [draft, setDraft] = useState(base);
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<{ tone: "good" | "bad" | "warn"; text: string } | null>(null);
  const [probe, setProbe] = useState<{ busy: boolean; result?: StreamProbe; error?: string }>({ busy: false });
  const [profiles, setProfiles] = useState<{ busy: boolean; list?: OnvifProfile[]; error?: string }>({ busy: false });
  const generation = useRef(0);
  const nameChanged = draft.name.trim() !== base.name;
  const sourceChanged = SOURCE_FIELDS.some(field => draft[field] !== base[field]);
  const dirty = nameChanged || sourceChanged;

  // A status refresh updates published values but never replaces edits in progress.
  useEffect(() => {
    const next = published(camera);
    setBase(previous => (JSON.stringify(previous) === JSON.stringify(next) ? previous : next));
    setDraft(current => (JSON.stringify(current) === JSON.stringify(base) ? next : current));
  }, [camera]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    generation.current += 1; // answers for a camera that is no longer open are ignored
  }, []);

  const change = <K extends keyof Draft>(field: K, value: Draft[K]) => {
    setDraft(current => ({ ...current, [field]: value }));
    setOutcome(null);
  };

  async function save(): Promise<boolean> {
    if (!dirty || saving) return !dirty;
    if (!draft.name.trim()) {
      setOutcome({ tone: "bad", text: "Camera name is required." });
      return false;
    }
    const mine = ++generation.current;
    setSaving(true);
    const saved: string[] = [];
    const failed: string[] = [];
    let latest: Camera | null = null;
    if (nameChanged) {
      try {
        latest = await renameCamera(camera.id, draft.name.trim());
        saved.push("name");
        setBase(current => ({ ...current, name: latest!.name }));
      } catch (error) {
        failed.push(`Name not saved: ${detail(error, "the NVR could not be reached")}`);
      }
    }
    if (sourceChanged) {
      try {
        latest = await updateSource(camera.id, sourceOf(draft));
        saved.push("connection");
        const next = published(latest);
        setBase(current => ({ ...next, name: current.name }));
        setDraft(current => ({ ...next, name: current.name }));
      } catch (error) {
        failed.push(`Connection not saved: ${detail(error, "the NVR could not be reached")}`);
      }
    }
    if (mine !== generation.current) return false;
    setSaving(false);
    void client.invalidateQueries({ queryKey: keys.cameras });
    if (failed.length) {
      setOutcome({ tone: saved.length ? "warn" : "bad", text: `${saved.length ? `Saved ${saved.join(" and ")}. ` : ""}${failed.join(" ")}` });
      return false;
    }
    setOutcome({ tone: "good", text: `Saved ${saved.join(" and ")}. Recording, motion, AI and schedule settings were not changed.` });
    return true;
  }

  function revert() {
    setDraft(base);
    setOutcome(null);
    setProbe({ busy: false });
  }

  useDraft(dirty && admin, { label: `General & Connection for ${camera.name}`, save, discard: revert });

  async function test() {
    const mine = generation.current;
    setProbe({ busy: true });
    try {
      const result = await testSource(camera.id, sourceOf(draft));
      if (mine === generation.current) setProbe({ busy: false, result });
    } catch (error) {
      if (mine === generation.current) setProbe({ busy: false, error: detail(error, "The stream test could not run.") });
    }
  }

  async function fetchProfiles() {
    const mine = generation.current;
    setProfiles({ busy: true });
    try {
      const result = await loadProfiles(camera.id, sourceOf(draft));
      if (mine !== generation.current) return;
      setProfiles({ busy: false, list: result.profiles });
      if (result.device_service_url && !draft.device_service_url) change("device_service_url", result.device_service_url);
    } catch (error) {
      if (mine === generation.current) setProfiles({ busy: false, error: detail(error, "Profiles could not be loaded.") });
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void save();
  }

  if (!admin) {
    return (
      <dl className="details">
        <dt>Name</dt><dd>{camera.name}</dd>
        <dt>Address</dt><dd className="mono">{camera.camera_ip ?? camera.stream_url}</dd>
        <dt>Brand</dt><dd>{BRAND_LABEL[camera.camera_brand ?? "auto"]}</dd>
        <dt>Main profile</dt><dd>{camera.onvif_profile_token ?? "Not reported"}</dd>
        <dt>Live substream</dt><dd>{camera.live_substream_available ? camera.live_substream_profile_token ?? "Configured" : "None"}</dd>
        <dt>Video</dt><dd>{camera.video.codec ? `${camera.video.codec.toUpperCase()} ${camera.video.width ?? "?"}×${camera.video.height ?? "?"}` : "Not reported"}</dd>
      </dl>
    );
  }

  const profileOptions = profiles.list ?? [];
  const knownTokens = new Set(profileOptions.map(profile => profile.token));
  return (
    <form className="tab-form" onSubmit={submit} aria-label="General and connection settings" noValidate>
      <label className="field">
        <span>Name</span>
        <input value={draft.name} onChange={event => change("name", event.target.value)} required maxLength={120} />
      </label>

      <fieldset className="field-group">
        <legend>Connection</legend>
        <div className="segmented" role="radiogroup" aria-label="Connect by">
          <label><input type="radio" name="mode" checked={draft.mode === "onvif"} onChange={() => change("mode", "onvif")} /> Camera address (ONVIF)</label>
          <label><input type="radio" name="mode" checked={draft.mode === "url"} onChange={() => change("mode", "url")} /> Direct RTSP URL</label>
        </div>
        {draft.mode === "onvif" ? (
          <>
            <label className="field"><span>IP address</span>
              <input className="mono" value={draft.camera_ip} onChange={event => change("camera_ip", event.target.value)} inputMode="decimal" autoComplete="off" />
            </label>
            <label className="field"><span>Brand</span>
              <select value={draft.camera_brand} onChange={event => change("camera_brand", event.target.value as CameraBrand)}>
                {(Object.keys(BRAND_LABEL) as CameraBrand[]).map(brand => <option key={brand} value={brand}>{brand === "auto" ? "Detect automatically" : BRAND_LABEL[brand]}</option>)}
              </select>
            </label>
            <label className="field"><span>ONVIF device service (optional)</span>
              <input className="mono" value={draft.device_service_url} onChange={event => change("device_service_url", event.target.value)} placeholder="Discovered automatically" autoComplete="off" />
            </label>
          </>
        ) : (
          <label className="field"><span>RTSP URL</span>
            <input className="mono" value={draft.stream_url} onChange={event => change("stream_url", event.target.value)}
                   placeholder={`Saved: ${camera.stream_url} (enter the full URL to change it)`} autoComplete="off" />
          </label>
        )}
      </fieldset>

      <fieldset className="field-group">
        <legend>Credentials</legend>
        <p className="muted field-hint">Saved credentials are never shown. Leave both blank to keep them; enter both to replace them.</p>
        <label className="field"><span>Username</span>
          <input value={draft.username} onChange={event => change("username", event.target.value)} autoComplete="off" disabled={draft.clear_credentials} />
        </label>
        <label className="field"><span>Password</span>
          <input type="password" value={draft.password} onChange={event => change("password", event.target.value)} autoComplete="new-password" disabled={draft.clear_credentials} />
        </label>
        <label className="check"><input type="checkbox" checked={draft.clear_credentials}
               onChange={event => setDraft(current => ({ ...current, clear_credentials: event.target.checked, username: "", password: "" }))} /> Remove saved credentials</label>
      </fieldset>

      {draft.mode === "onvif" ? (
        <fieldset className="field-group">
          <legend>Streams</legend>
          <div className="field-actions">
            <button type="button" className="button" onClick={() => void fetchProfiles()} disabled={profiles.busy || !draft.camera_ip.trim()}>
              {profiles.busy ? "Loading profiles…" : "Load profiles"}
            </button>
          </div>
          {profiles.error ? <p className="field-error" role="alert">{profiles.error}</p> : null}
          <label className="field"><span>Main (recording) profile</span>
            <select value={draft.onvif_profile_token} onChange={event => change("onvif_profile_token", event.target.value)}>
              <option value="">Camera default</option>
              {draft.onvif_profile_token && !knownTokens.has(draft.onvif_profile_token) ? <option value={draft.onvif_profile_token}>{draft.onvif_profile_token}</option> : null}
              {profileOptions.map(profile => <option key={profile.token} value={profile.token}>{profile.name || profile.token}</option>)}
            </select>
          </label>
          <label className="field"><span>Live substream (optional, lower resolution)</span>
            <select value={draft.live_substream_profile_token} onChange={event => change("live_substream_profile_token", event.target.value)}>
              <option value="">None</option>
              {draft.live_substream_profile_token && !knownTokens.has(draft.live_substream_profile_token) ? <option value={draft.live_substream_profile_token}>{draft.live_substream_profile_token}</option> : null}
              {profileOptions.filter(profile => profile.token !== draft.onvif_profile_token).map(profile => <option key={profile.token} value={profile.token}>{profile.name || profile.token}</option>)}
            </select>
          </label>
        </fieldset>
      ) : null}

      <div className="field-actions">
        <button type="button" className="button" onClick={() => void test()} disabled={probe.busy}>{probe.busy ? "Testing…" : "Test stream"}</button>
        {probe.result ? (
          <span role="status" className={probe.result.reachable ? "test-ok" : "field-error"}>
            {probe.result.reachable
              ? `Stream opened: ${probe.result.video?.codec?.toUpperCase() ?? "video"} ${probe.result.video?.width ?? "?"}×${probe.result.video?.height ?? "?"}${probe.result.audio ? " with audio" : ""}. A test is not proof of recording.`
              : `Stream did not open: ${probe.result.reason ?? "no reason reported"}`}
          </span>
        ) : null}
        {probe.error ? <span className="field-error" role="alert">{probe.error}</span> : null}
      </div>

      {outcome ? <p className="notice" data-tone={outcome.tone} role={outcome.tone === "good" ? "status" : "alert"}>{outcome.text}</p> : null}
      <footer className="tab-actions">
        <button type="button" className="button" onClick={revert} disabled={!dirty || saving}>Revert</button>
        <button type="submit" className="button" data-variant="primary" disabled={!dirty || saving}>{saving ? "Saving…" : "Save"}</button>
      </footer>
    </form>
  );
}
