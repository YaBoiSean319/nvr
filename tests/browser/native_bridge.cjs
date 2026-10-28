// A stand-in for Capacitor's bridge and the plugins the interface uses (#140), injected into a page before it loads. It runs in the browser, so it must stay self-contained.
// The native-app journeys use it with the app's user agent; NVR_APP_MODE=1 runs the existing journeys the same way (see helpers.cjs and .github/workflows/mobile.yml).
const APP_AGENT = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36 NVRApp/1.0";

function installBridge() {
  if (window.__stub) return;
  const store = key => ({ get: () => JSON.parse(localStorage.getItem(key) || "{}"), set: value => localStorage.setItem(key, JSON.stringify(value)) });
  const prefs = store("stub.prefs");
  const creds = store("stub.creds");
  const listeners = {};
  const stub = { verify: { ok: !localStorage.getItem("stub.deny"), calls: 0 }, skew: 0, pushToken: "fcm-registration-token:" + "A".repeat(60), emit: (name, data) => (listeners[name] || []).forEach(callback => callback(data)) };
  window.__stub = stub;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + stub.skew;
  const on = (name, callback) => { (listeners[name] = listeners[name] || []).push(callback); return { remove() { listeners[name] = listeners[name].filter(item => item !== callback); } }; };
  window.Capacitor = {
    isNativePlatform: () => true,
    getPlatform: () => "android",
    Plugins: {
      Preferences: {
        get: async ({ key }) => ({ value: prefs.get()[key] ?? null }),
        set: async ({ key, value }) => { const all = prefs.get(); all[key] = value; prefs.set(all); },
        remove: async ({ key }) => { const all = prefs.get(); delete all[key]; prefs.set(all); },
      },
      NativeBiometric: {
        isAvailable: async () => ({ isAvailable: true }),
        verifyIdentity: async () => { stub.verify.calls += 1; if (!stub.verify.ok) throw new Error("cancelled"); },
        setCredentials: async ({ server, username, password }) => { const all = creds.get(); all[server] = { username, password }; creds.set(all); },
        getCredentials: async ({ server }) => { const found = creds.get()[server]; if (!found) throw new Error("no credentials"); return found; },
        deleteCredentials: async ({ server }) => { const all = creds.get(); delete all[server]; creds.set(all); },
      },
      PushNotifications: {
        requestPermissions: async () => ({ receive: "granted" }),
        register: async () => { setTimeout(() => stub.emit("registration", { value: stub.pushToken }), 0); },
        addListener: async (name, callback) => on(name, callback),
      },
      App: { addListener: async (name, callback) => on(name, callback) },
    },
  };
}

module.exports = { APP_AGENT, installBridge };
