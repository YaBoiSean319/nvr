// The installable app's service worker (#110). It keeps only the application shell: the page
// and the hashed assets it references. It never intercepts API, media or snapshot requests.
// It also shows alarm notifications (#111) and opens the alarm when one is clicked.
// The server replaces __VERSION__ with the built page's digest (app/api/web_interface.py), so
// each release installs a new worker; it takes over once the previous release's pages have
// closed, and then deletes that release's cache.
"use strict";
const VERSION = "__VERSION__";
const SHELL = `nvr-shell-${VERSION}`;

const isPage = response => response.ok && response.type === "basic" && (response.headers.get("Content-Type") || "").startsWith("text/html");

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    const page = await fetch(new Request("/", { cache: "reload" }));
    if (!isPage(page)) throw new Error("The application page is unavailable");
    // The page and every hashed file it names, so the shell also opens without the server.
    const html = await page.clone().text();
    const assets = [...new Set(html.match(/\/assets\/[\w.-]+/g) || [])];
    await cache.addAll(assets);
    await cache.put("/", page);
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith("nvr-shell-") && key !== SHELL).map(key => caches.delete(key)),
  )));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/assets/")) {
    // Hashed file names never change content, so the first copy is the right one.
    event.respondWith(caches.open(SHELL).then(cache => cache.match(request).then(hit => hit || fetch(request).then(response => {
      if (response.ok && response.type === "basic") cache.put(request, response.clone());
      return response;
    }))));
    return;
  }
  if (request.mode === "navigate" && !url.pathname.startsWith("/api/")) {
    // The page always comes from the network when it can, so a new release shows at once.
    event.respondWith(fetch(request).then(response => {
      if (isPage(response)) {
        const copy = response.clone();
        event.waitUntil(caches.open(SHELL).then(cache => cache.put("/", copy)));
      }
      return response;
    }).catch(() => caches.open(SHELL).then(cache => cache.match("/")).then(hit => hit || Response.error())));
  }
  // Everything else (API responses, live and recorded media, snapshots) is never touched.
});

self.addEventListener("push", event => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch {
    message = {};
  }
  const title = message.title || "Alarm";
  event.waitUntil(self.registration.showNotification(title, {
    body: message.body || "",
    // The thumbnail link is short-lived and names only this alarm; it carries no session.
    image: message.thumbnail_url || undefined,
    icon: message.thumbnail_url || undefined,
    tag: message.alarm_id ? `alarm-${message.alarm_id}` : undefined,
    data: { url: message.url || "/" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data && event.notification.data.url ? event.notification.data.url : "/", self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    const open = windows.find(client => new URL(client.url).origin === target.origin);
    // navigate() rejects for a window this worker doesn't control yet; open a fresh one instead.
    if (open) return open.navigate(target.href).then(client => (client || open).focus()).catch(() => self.clients.openWindow(target.href));
    return self.clients.openWindow(target.href);
  }));
});
