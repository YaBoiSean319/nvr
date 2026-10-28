/** The app keeps a device secret in the phone's Keychain or Keystore, so it can sign the person back in after unlocking instead of asking for the password (#140). */
import { api } from "@/api/client";
import { plugin } from "./platform";

const USERNAME = "nvr-device-session";

const server = () => window.location.origin;

async function stored(): Promise<string | null> {
  try {
    const credentials = (await plugin("NativeBiometric")?.getCredentials({ server: server() })) as { password?: string } | undefined;
    return credentials?.password ?? null;
  } catch {
    return null;
  }
}

async function keep(token: string): Promise<void> {
  await plugin("NativeBiometric")?.setCredentials({ username: USERNAME, password: token, server: server() });
}

/** After a normal sign-in: ask the NVR for a device secret and keep it in secure storage. */
export async function rememberThisPhone(label: string): Promise<void> {
  if (await stored()) return;
  const created = await api<{ token: string }>("/api/native/device-sessions", { method: "POST", body: { device_label: label } });
  await keep(created.token);
}

/** When the session has ended: sign in with the stored secret, and keep the replacement the NVR sends back. False when there is none or it no longer works. */
export async function resumeFromSecureStorage(): Promise<boolean> {
  const token = await stored();
  if (!token) return false;
  try {
    const resumed = await api<{ token: string }>("/api/native/device-sessions/resume", { method: "POST", body: { token } });
    await keep(resumed.token);
    return true;
  } catch {
    await forgetThisPhone(false);
    return false;
  }
}

/** Sign-out on the phone: the stored secret is deleted, and with `revoke` the NVR forgets it too. */
export async function forgetThisPhone(revoke = true): Promise<void> {
  const token = await stored();
  try {
    await plugin("NativeBiometric")?.deleteCredentials({ server: server() });
  } catch {
    /* Nothing was stored. */
  }
  if (revoke && token) {
    try {
      await api("/api/native/device-sessions/revoke", { method: "POST", body: { token } });
    } catch {
      /* Signed out already: the secret expires by itself. */
    }
  }
}
