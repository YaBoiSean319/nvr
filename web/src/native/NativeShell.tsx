import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { biometricsAvailable, loadLastActive, loadLockSettings, saveLastActive, shouldLock, unlock, type LockSettings } from "./lock";
import { isNativeApp, plugin } from "./platform";

const ACTIVITY_EVENTS = ["pointerdown", "keydown", "touchstart", "wheel"] as const;
const CHECK_MS = 5_000;
const SAVE_EVERY_MS = 15_000;

/**
 * Around the whole interface when it runs as the native app: locks it behind the phone's biometrics once the person has been idle for the time they chose,
 * on opening the app after that time, and on coming back to it. A locked app renders none of the interface, so nothing of it is on screen (or in the app switcher).
 */
export function NativeShell({ children }: { children: ReactNode }) {
  if (!isNativeApp()) return <>{children}</>;
  return <Locked>{children}</Locked>;
}

function Locked({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<LockSettings | null>(null);
  const [locked, setLocked] = useState(true);
  const [failed, setFailed] = useState(false);
  const lastActive = useRef(Date.now());
  const savedAt = useRef(0);
  const asking = useRef(false);

  const touch = useCallback(() => {
    const now = Date.now();
    lastActive.current = now;
    if (now - savedAt.current > SAVE_EVERY_MS) {
      savedAt.current = now;
      void saveLastActive(now);
    }
  }, []);

  // Start: read the settings and when the person was last here, then lock if they were away too long.
  useEffect(() => {
    void (async () => {
      const [loaded, last] = await Promise.all([loadLockSettings(), loadLastActive()]);
      lastActive.current = last;
      setSettings(loaded);
      setLocked(shouldLock(loaded, last, Date.now()));
    })();
  }, []);

  // The settings can change while the app is open (the person turns the lock on or changes the time).
  useEffect(() => {
    const reload = () => void loadLockSettings().then(setSettings);
    window.addEventListener("nvr-lock-settings", reload);
    return () => window.removeEventListener("nvr-lock-settings", reload);
  }, []);

  const check = useCallback(() => {
    if (settings && shouldLock(settings, lastActive.current, Date.now())) setLocked(true);
  }, [settings]);

  useEffect(() => {
    if (!settings || locked) return;
    const options = { passive: true, capture: true } as const;
    for (const name of ACTIVITY_EVENTS) window.addEventListener(name, touch, options);
    const timer = window.setInterval(check, CHECK_MS);
    const app = plugin("App");
    const listener = app?.addListener("appStateChange", (state: { isActive: boolean }) => {
      if (state.isActive) check();
      else void saveLastActive(lastActive.current);
    });
    return () => {
      for (const name of ACTIVITY_EVENTS) window.removeEventListener(name, touch, options);
      window.clearInterval(timer);
      void Promise.resolve(listener).then(handle => handle?.remove());
    };
  }, [settings, locked, touch, check]);

  const ask = useCallback(async () => {
    if (asking.current) return;
    asking.current = true;
    setFailed(false);
    try {
      if (await unlock()) {
        setLocked(false);
        touch();
      } else setFailed(true);
    } finally {
      asking.current = false;
    }
  }, [touch]);

  // Locking is only possible when the phone can check the person: otherwise the lock would be a door with no key.
  useEffect(() => {
    if (locked && settings?.enabled) void biometricsAvailable().then(available => (available ? ask() : setLocked(false)));
    else if (locked && settings) setLocked(false);
  }, [locked, settings, ask]);

  if (!settings) return <div className="app-loading" role="status"><span className="spinner" /> <span>Loading…</span></div>;
  if (locked) {
    return (
      <div className="app-lock" role="dialog" aria-modal="true" aria-label="NVR is locked">
        <h1>NVR is locked</h1>
        <p className="muted">{failed ? "That did not unlock it." : "Unlock with your face, fingerprint or passcode."}</p>
        <button type="button" className="button" data-variant="primary" onClick={() => void ask()}>Unlock</button>
      </div>
    );
  }
  return <>{children}</>;
}
