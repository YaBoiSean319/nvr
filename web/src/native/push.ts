/** Native push (#140): the phone's FCM or APNs token is registered with the NVR under the signed-in person, and a tap on a notification opens its alarm. */
import { api } from "@/api/client";
import { deviceLabel } from "@/features/alarms/pushDevice";
import { nativePlatform, plugin, readSetting, writeSetting } from "./platform";

const KEY_ENABLED = "push.enabled";
const KEY_TOKEN = "push.token";

export const nativePushEnabled = async (): Promise<boolean> => (await readSetting(KEY_ENABLED)) === "1";

/** Ask the phone for permission and a token. Rejects with a sentence the person can read. */
function obtainToken(): Promise<string> {
  const push = plugin("PushNotifications");
  if (!push) return Promise.reject(new Error("This app cannot receive notifications on this phone."));
  return new Promise((resolve, reject) => {
    void (async () => {
      const handles = [
        await push.addListener("registration", (token: { value: string }) => { cleanup(); resolve(token.value); }),
        await push.addListener("registrationError", () => { cleanup(); reject(new Error("The phone could not register for notifications.")); }),
      ];
      const cleanup = () => handles.forEach(handle => void handle.remove());
      const permission = (await push.requestPermissions()) as { receive: string };
      if (permission.receive !== "granted") {
        cleanup();
        reject(new Error("Notifications are blocked for the app in the phone's settings."));
        return;
      }
      await push.register();
    })().catch(error => reject(error instanceof Error ? error : new Error("The phone could not register for notifications.")));
  });
}

async function send(token: string): Promise<void> {
  await api("/api/native/devices", { method: "POST", body: { platform: nativePlatform(), token, device_label: deviceLabel() } });
  await writeSetting(KEY_TOKEN, token);
}

export async function enableNativePush(): Promise<void> {
  await send(await obtainToken());
  await writeSetting(KEY_ENABLED, "1");
}

export async function disableNativePush(): Promise<void> {
  const token = await readSetting(KEY_TOKEN);
  if (token) await api("/api/native/devices/remove", { method: "POST", body: { token } }).catch(() => undefined);
  await writeSetting(KEY_ENABLED, null);
  await writeSetting(KEY_TOKEN, null);
}

/** Tokens change: each time the app opens for a signed-in person who turned notifications on, say again which token is current. */
export async function refreshNativePush(): Promise<void> {
  if (!(await nativePushEnabled())) return;
  try {
    await send(await obtainToken());
  } catch {
    /* The settings page shows the state; opening the app must not fail over it. */
  }
}

/** Open the alarm a tapped notification is about. Only paths on this NVR are followed. */
export async function watchNotificationTaps(open: (path: string) => void): Promise<() => void> {
  const push = plugin("PushNotifications");
  if (!push) return () => undefined;
  const handle = await push.addListener("pushNotificationActionPerformed", (action: { notification?: { data?: { url?: string } } }) => {
    const url = action.notification?.data?.url;
    if (typeof url === "string" && url.startsWith("/") && !url.startsWith("//")) open(url);
  });
  return () => void handle.remove();
}
