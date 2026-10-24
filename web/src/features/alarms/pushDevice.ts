import { api } from "@/api/client";

export const pushSupported = () => typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

/** This device's subscription, if the browser has one for this site. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.getRegistration("/");
  return registration ? registration.pushManager.getSubscription() : null;
}

/** Stop this device receiving the signed-in person's notifications. */
export async function forgetThisDevice(): Promise<void> {
  const existing = await currentSubscription();
  if (!existing) return;
  await api("/api/push/subscriptions/remove", { method: "POST", body: { endpoint: existing.endpoint } });
  await existing.unsubscribe();
}

/** A coarse name for this device, so a person can tell their devices apart without sharing the full user agent. */
export function deviceLabel(): string {
  const agent = navigator.userAgent;
  const system = /Android/.test(agent) ? "Android" : /iPhone|iPad|iPod/.test(agent) ? "iOS" : /Mac OS X/.test(agent) ? "macOS" : /Windows/.test(agent) ? "Windows" : /CrOS/.test(agent) ? "ChromeOS" : /Linux/.test(agent) ? "Linux" : "Unknown system";
  const browser = /Edg\//.test(agent) ? "Edge" : /Firefox\//.test(agent) ? "Firefox" : /Chrome\//.test(agent) ? "Chrome" : /Safari\//.test(agent) ? "Safari" : "Browser";
  return `${browser} on ${system}`;
}
