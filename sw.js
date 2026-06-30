/* C3D Prints Admin - service worker
   Makes the dashboard installable on desktop and viewable offline.
   Strategy: network-first with cache fallback. Successful GETs are cached, so the
   last-loaded app + data are available offline; when online, the network response
   is used and the cache refreshed (auto-sync). Mutations (POST/PATCH/DELETE) are
   never cached - they require a connection. */

const CACHE = "c3d-admin-v1";
const API_ORIGIN = "https://c3dprints-quote-portal.onrender.com";
const APP_SHELL = [
  "./admin.html",
  "./manifest.webmanifest",
  "./logo.png",
  "./icons/icon-180.png",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // let mutations go straight to the network

  const url = new URL(req.url);
  const isApi = url.origin === API_ORIGIN;

  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((cached) => {
          if (cached) return cached;
          if (isApi) {
            return new Response(JSON.stringify({ offline: true }), {
              status: 503,
              headers: { "Content-Type": "application/json" },
            });
          }
          return caches.match("./admin.html");
        })
      )
  );
});
