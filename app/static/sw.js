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

self.addEventListener('push', function(event) {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { body: event.data.text() };
    }
  }

  const title = data.title || 'Cleverly Connected Meals';
  const options = {
    body: data.body || '',
    icon: '/static/img/ccm-icon-192.png',
    badge: '/static/img/ccm-icon-64.png',
    data: {
      url: data.url || '/'
    }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
    for (const client of clientList) {
      if (client.url.includes(new URL(targetUrl, self.location.origin).pathname)) {
        return client.focus();
      }
    }
    return clients.openWindow(targetUrl);
  }));
});
