/** The app keeps a device secret in the phone's Keychain or Keystore, so it can sign the person back in after unlocking instead of asking for the password (#140). */
import { api, ApiError } from "@/api/client";
import { plugin } from "./platform";

const server = () => window.location.origin;

interface Stored { username: string; password: string }

async function stored(): Promise<Stored | null> {
  try {
    const credentials = (await plugin("NativeBiometric")?.getCredentials({ server: server() })) as Partial<Stored> | undefined;
    return credentials?.password ? { username: credentials.username ?? "", password: credentials.password } : null;
  } catch {
    return null;
  }
}

/** The secret is kept under the name of the person it belongs to, so one person's is never taken for another's. */
async function keep(username: string, token: string): Promise<void> {
  await plugin("NativeBiometric")?.setCredentials({ username, password: token, server: server() });
}

async function erase(): Promise<void> {
  try {
    await plugin("NativeBiometric")?.deleteCredentials({ server: server() });
  } catch {
    /* Nothing was stored. */
  }
}

const revoke = (token: string) => api("/api/native/device-sessions/revoke", { method: "POST", body: { token } }).catch(() => undefined);

/** After a normal sign-in: ask the NVR for a device secret and keep it in secure storage, replacing one that belongs to someone else. */
export async function rememberThisPhone(username: string, label: string): Promise<void> {
  const existing = await stored();
  if (existing?.username === username) return;
  if (existing) {
    await erase();
    await revoke(existing.password);
  }
  const created = await api<{ token: string }>("/api/native/device-sessions", { method: "POST", body: { device_label: label } });
  try {
    await keep(username, created.token);
  } catch (error) {
    await revoke(created.token);  // Not kept, so of no use to anyone: do not leave it counting against the person's phones
    throw error;
  }
}

/** When the session has ended: sign in with the stored secret, and keep the replacement the NVR sends back. False when there is none or it no longer works. */
export async function resumeFromSecureStorage(): Promise<boolean> {
  const existing = await stored();
  if (!existing) return false;
  try {
    const resumed = await api<{ username: string; token: string }>("/api/native/device-sessions/resume", { method: "POST", body: { token: existing.password } });
    await keep(resumed.username, resumed.token);
    return true;
  } catch (error) {
    // Only the NVR saying the secret is no good forgets it; being offline or the NVR being down must not sign the phone out for good.
    if (error instanceof ApiError && error.unauthorized) await erase();
    return false;
  }
}

/** Sign-out on the phone: the stored secret is deleted, and with `revokeIt` the NVR forgets it too. */
export async function forgetThisPhone(revokeIt = true): Promise<void> {
  const existing = await stored();
  await erase();
  if (revokeIt && existing) await revoke(existing.password);
}
