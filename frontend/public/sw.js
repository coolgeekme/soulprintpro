/* Foundry — self-unregistering service worker.
 *
 * Why this exists as a stub:
 *   We previously shipped a network-first SW that cached the app shell. As
 *   the app evolves quickly, stuck SW installs produced "blank page after
 *   login" / "white screen on /about" reports because old shells were
 *   served while new bundle hashes 404'd.
 *
 * What this does instead:
 *   On install, this SW immediately unregisters itself and deletes every
 *   cache it owns. After the user's next reload the SW is gone and the
 *   app behaves like any normal SPA.
 *
 * Registration in index.html is intentionally retained so users on the
 * old buggy SW automatically pick this up and self-heal on their next
 * page load. After this SW unregisters, future navigations re-register
 * a fresh copy that simply unregisters again — there is no caching,
 * intercept, or reload loop.
 *
 * If we want PWA install + offline support back, we'll re-introduce a
 * versioned SW once the app's release cadence stabilizes.
 */
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    } catch (_) { /* ignore */ }
    try {
      await self.registration.unregister();
    } catch (_) { /* ignore */ }
    // Deliberately NOT calling clients.navigate here — that would create a
    // reload loop with index.html's unconditional register() on load.
  })());
});

// Pass every fetch straight to the network. No caching, no shell fallback.
self.addEventListener("fetch", () => { /* default network behaviour */ });
