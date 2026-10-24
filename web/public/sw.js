// The installable app's service worker (#110). It keeps only the application shell: the page
// and the hashed assets it references. It never intercepts API, media or snapshot requests.
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
