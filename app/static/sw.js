// Minimal service worker for basic PWA installability.
// This keeps a fetch handler so the site can be controlled by the service worker
// once installed, but it intentionally does not implement caching strategies.

self.addEventListener('install', function(event) {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', function(event) {
  event.waitUntil(self.clients.claim());
});

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) {
    output[index] = raw.charCodeAt(index);
  }
  return output;
}

async function resubscribeForPush(event) {
  let subscription = event.newSubscription || await self.registration.pushManager.getSubscription();
  if (!subscription) {
    const options = event.oldSubscription?.options;
    if (options?.applicationServerKey) {
      subscription = await self.registration.pushManager.subscribe(options);
    } else {
      const response = await fetch('/push/public_key', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok || response.redirected) throw new Error('Unable to load push settings');
      const { publicKey } = await response.json();
      subscription = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey)
      });
    }
  }

  const response = await fetch('/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      subscription: subscription.toJSON(),
      previousEndpoint: event.oldSubscription?.endpoint || null
    })
  });
  if (!response.ok || response.redirected || !(await response.json()).ok) {
    throw new Error('CCM did not confirm the subscription');
  }
}

self.addEventListener('pushsubscriptionchange', function(event) {
  event.waitUntil(resubscribeForPush(event).catch(function(error) {
    console.warn('Push subscription refresh failed', error);
  }));
});

self.addEventListener('fetch', function(event) {
  // Default: just allow network to proceed. This keeps the SW minimal and safe.
});

self.addEventListener('push', function(event) {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json() || {};
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

  if (data.id) options.tag = data.id;
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil((async function() {
    const origin = self.location.origin;
    const target = new URL(event.notification.data?.url || '/', origin);
    const url = target.origin === origin ? target.href : origin + '/';
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const existing = windows.find(client => client.url === url);
    if (existing) return existing.focus();
    return self.clients.openWindow(url);
  })());
});
