import { useEffect, useState } from "react";
import { biometricsAvailable, DEFAULT_IDLE_MINUTES, IDLE_CHOICES, loadLockSettings, saveLastActive, saveLockSettings, type LockSettings } from "./lock";
import { isNativeApp, nativeHomeUrl, writeSetting } from "./platform";
import { forgetThisPhone } from "./session";

/** In the native app only: the biometric lock and how long the app may sit idle before it asks again. */
export function NativeDeviceSettings() {
  const [settings, setSettings] = useState<LockSettings>({ enabled: false, idleMinutes: DEFAULT_IDLE_MINUTES });
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    if (!isNativeApp()) return;
    void loadLockSettings().then(setSettings);
    void biometricsAvailable().then(setAvailable);
  }, []);
  if (!isNativeApp()) return null;

  const change = (next: LockSettings) => {
    setSettings(next);
    void saveLockSettings(next).then(() => {
      // Turning the lock on counts from now, so it does not lock the person out at once.
      if (next.enabled) return saveLastActive(Date.now());
      return undefined;
    }).then(() => window.dispatchEvent(new Event("nvr-lock-settings")));
  };

  return (
    <fieldset className="plain" aria-label="This phone">
      <legend className="field-heading">This phone</legend>
      <label className="check">
        <input type="checkbox" checked={settings.enabled} disabled={available === false} onChange={event => change({ ...settings, enabled: event.target.checked })} /> Lock the app with face, fingerprint or passcode
      </label>
      <label className="field"><span>Lock after being idle for</span>
        <select value={settings.idleMinutes} disabled={!settings.enabled} onChange={event => change({ ...settings, idleMinutes: Number(event.target.value) })}>
          {IDLE_CHOICES.map(minutes => <option key={minutes} value={minutes}>{minutes === 1 ? "1 minute" : `${minutes} minutes`}</option>)}
        </select>
      </label>
      {available === false ? <p className="muted field-hint">This phone has no face, fingerprint or passcode set up, so the app cannot be locked.</p> : null}
      <div className="field-actions">
        <button type="button" className="button" onClick={() => void (async () => {
          // Forget this NVR on the phone (its sign-in secret too) and go back to the start screen, which asks for an address.
          await forgetThisPhone().catch(() => undefined);
          await writeSetting("server.url", null);
          window.location.assign(nativeHomeUrl());
        })()}>Use a different NVR</button>
      </div>
      <p className="muted field-hint">The app keeps no footage or pictures on the phone once you leave it. Your sign-in is kept in the phone's secure storage.</p>
    </fieldset>
  );
}
