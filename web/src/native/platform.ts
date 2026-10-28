/**
 * The native mobile app (#140) wraps this interface in a Capacitor shell. Capacitor injects `window.Capacitor` and its plugins into the page, so
 * nothing here is bundled from Capacitor: in a browser every function below says "not the app" and does nothing.
 */
export type NativePlatform = "android" | "ios";

interface Listener { remove(): Promise<void> | void }
export interface PluginHandle {
  addListener(event: string, callback: (data: any) => void): Promise<Listener> | Listener;
  [method: string]: any;
}
interface CapacitorGlobal {
  isNativePlatform?: () => boolean;
  getPlatform?: () => string;
  Plugins?: Record<string, PluginHandle | undefined>;
}

const capacitor = (): CapacitorGlobal | undefined => (window as unknown as { Capacitor?: CapacitorGlobal }).Capacitor;

/** True inside the app, whose user agent also says so (the server then marks everything it sends as not to be cached). */
export const isNativeApp = (): boolean => typeof window !== "undefined" && Boolean(capacitor()?.isNativePlatform?.()) && /NVRApp\//.test(navigator.userAgent);

export const nativePlatform = (): NativePlatform => (capacitor()?.getPlatform?.() === "ios" ? "ios" : "android");

export const plugin = (name: string): PluginHandle | undefined => capacitor()?.Plugins?.[name];

/** Settings kept natively (shared with the app's start screen), not in the page's own storage. */
export async function readSetting(key: string): Promise<string | null> {
  const preferences = plugin("Preferences");
  return preferences ? ((await preferences.get({ key })) as { value: string | null }).value : null;
}

export async function writeSetting(key: string, value: string | null): Promise<void> {
  const preferences = plugin("Preferences");
  if (!preferences) return;
  if (value === null) await preferences.remove({ key });
  else await preferences.set({ key, value });
}

/** The app's own start screen (it asks for the NVR's address), served from inside the app. */
export const nativeHomeUrl = (): string => (nativePlatform() === "ios" ? "capacitor://localhost/" : "https://localhost/");
