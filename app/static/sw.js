// Minimal service worker for basic PWA installability.
// This keeps a fetch handler so the site can be controlled by the service worker
// once installed, but it intentionally does not implement caching strategies.

self.addEventListener('install', function(event) {
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function(event) {
  // Default: just allow network to proceed. This keeps the SW minimal and safe.
});
