const CACHE = 'ale-2026-visual-v2';
const STATIC_ASSETS = [
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

// Install the new worker immediately. Do not pre-cache index.html;
// HTML/navigation requests use network-first so GitHub updates appear promptly.
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(STATIC_ASSETS)).catch(() => {})
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== CACHE).map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Never cache dynamic reviewer/update JSON or versioned question banks.
  // This prevents stale Sir Brian mock banks, trend notes, and update metadata.
  if (
    /\/updates\/[^/]+\.json$/i.test(url.pathname) ||
    /\/question-bank-v[^/]+\.json$/i.test(url.pathname)
  ) {
    event.respondWith(fetch(req, { cache: 'no-store' }));
    return;
  }

  // Network-first for pages/HTML so a newly uploaded index.html is seen.
  if (req.mode === 'navigate' || url.pathname.endsWith('/index.html') || url.pathname.endsWith('/ALE-2026-Reviewer/')) {
    event.respondWith(
      fetch(req, { cache: 'no-store' })
        .then(response => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then(cache => cache.put(req, copy));
          }
          return response;
        })
        .catch(() => caches.match(req).then(r => r || caches.match('./index.html')))
    );
    return;
  }

  // Cache-first is fine for icons/manifest and other static assets.
  event.respondWith(
    caches.match(req).then(cached => {
      if (cached) return cached;
      return fetch(req).then(response => {
        if (response && response.ok && url.origin === self.location.origin) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(req, copy));
        }
        return response;
      });
    })
  );
});
