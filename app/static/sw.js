// Minimal service worker for basic PWA installability.
// This keeps a fetch handler so the site can be controlled by the service worker
// once installed, but it intentionally does not implement caching strategies.

self.addEventListener('install', function(event) {
  self.skipWaiting();
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

async function syncSubscription(subscription) {
  if (!subscription) {
    return;
  }

  await fetch('/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ subscription: subscription.toJSON() })
  });
}

async function resubscribeForPush(event) {
  const publicKeyResponse = await fetch('/push/public_key', { credentials: 'same-origin' });
  const publicKeyPayload = await publicKeyResponse.json();
  if (!publicKeyPayload.publicKey) {
    return;
  }

  const subscription = await self.registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKeyPayload.publicKey)
  });

  await syncSubscription(subscription);

  if (event.oldSubscription && event.oldSubscription.endpoint) {
    await fetch('/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ endpoint: event.oldSubscription.endpoint })
    });
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
