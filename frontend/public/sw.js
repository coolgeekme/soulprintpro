/* SoulPrint Engine — minimal install-only service worker.
 *
 * Goal: make the app installable as a PWA without inadvertently caching
 * user-specific API responses. We let the browser handle network fetches
 * normally and only intercept navigation requests with a tiny offline
 * fallback so the app still resolves an installable shell.
 */
const VERSION = "soulprint-v1";
const CORE = ["/", "/index.html", "/soulprint-logo.png", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(CORE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Don't cache API or auth — always go to network.
  if (url.pathname.startsWith("/api/") || url.hostname !== self.location.hostname) {
    return;
  }
  // For navigations, serve cached shell on offline; otherwise network-first.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/") || caches.match("/index.html"))
    );
    return;
  }
  // For static same-origin assets, cache-first with network fallback.
  event.respondWith(
    caches.match(req).then((cached) =>
      cached ||
      fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
            const copy = res.clone();
            caches.open(VERSION).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => cached)
    )
  );
});
