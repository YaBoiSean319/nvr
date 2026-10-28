/** The app's biometric lock (#140): after the idle time the person has set, the app hides itself until the phone's biometrics (or passcode) unlock it. */
import { plugin, readSetting, writeSetting } from "./platform";

export const IDLE_CHOICES = [1, 5, 15, 30, 60] as const;
export const DEFAULT_IDLE_MINUTES = 5;
const KEY_ENABLED = "lock.enabled";
const KEY_IDLE = "lock.idle_minutes";
const KEY_ACTIVE = "lock.last_active";

export interface LockSettings { enabled: boolean; idleMinutes: number }

/** Whether the app should be locked: the lock is on and the person has been away for the idle time or longer. */
export function shouldLock(settings: LockSettings, lastActive: number, now: number): boolean {
  return settings.enabled && now - lastActive >= settings.idleMinutes * 60_000;
}

export async function loadLockSettings(): Promise<LockSettings> {
  const idle = Number(await readSetting(KEY_IDLE));
  return { enabled: (await readSetting(KEY_ENABLED)) === "1", idleMinutes: (IDLE_CHOICES as readonly number[]).includes(idle) ? idle : DEFAULT_IDLE_MINUTES };
}

export async function saveLockSettings(settings: LockSettings): Promise<void> {
  await writeSetting(KEY_ENABLED, settings.enabled ? "1" : "0");
  await writeSetting(KEY_IDLE, String(settings.idleMinutes));
}

/** When the person last touched the app, kept natively so that coming back after the phone was away (or the app was closed) is measured from then. */
export async function loadLastActive(): Promise<number> {
  const stored = Number(await readSetting(KEY_ACTIVE));
  return Number.isFinite(stored) && stored > 0 ? stored : Date.now();
}

export const saveLastActive = (at: number): Promise<void> => writeSetting(KEY_ACTIVE, String(at));

/** Whether the phone can check a face, a fingerprint or a passcode. */
export async function biometricsAvailable(): Promise<boolean> {
  try {
    return Boolean(((await plugin("NativeBiometric")?.isAvailable({ useFallback: true })) as { isAvailable?: boolean } | undefined)?.isAvailable);
  } catch {
    return false;
  }
}

/** Ask the phone to check the person. True only when it says yes. */
export async function unlock(reason = "Unlock the NVR"): Promise<boolean> {
  const biometric = plugin("NativeBiometric");
  if (!biometric) return false;
  try {
    await biometric.verifyIdentity({ reason, title: "NVR", useFallback: true, maxAttempts: 3 });
    return true;
  } catch {
    return false;
  }
}
