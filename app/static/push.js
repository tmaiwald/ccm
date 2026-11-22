(function() {
  const config = document.currentScript.dataset;
  const serviceWorkers = navigator.serviceWorker;
  const supported = window.isSecureContext && serviceWorkers && 'Notification' in window && 'PushManager' in window;
  const registration = window.isSecureContext && serviceWorkers
    ? serviceWorkers.register(config.workerUrl).catch(error => {
        console.warn('CCM service worker registration failed', error);
        return null;
      })
    : Promise.resolve(null);

  if (!config.userId) return;

  let prepared;
  let pending;
  let previousEndpoint;
  const previousEndpointKey = 'ccm-push-previous-' + config.userId;

  async function request(url, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    const response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      ...(body ? {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      } : {})
    }).finally(() => clearTimeout(timeout));
    if (response.redirected || response.status === 401) {
      throw new Error('Sign in to CCM again to manage notifications.');
    }
    const data = await response.json().catch(() => null);
    if (!response.ok || !data || data.ok === false) {
      const error = new Error(data?.error || 'CCM could not save or verify notifications. Try again.');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function prepare() {
    if (!supported) throw new Error('Push notifications are unavailable here.');
    const reg = await registration;
    if (!reg) throw new Error('CCM could not start the service worker. Reload and try again.');
    const active = reg.active ? reg : await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('The service worker is still starting. Try again.')), 10000);
      serviceWorkers.ready.then(result => {
        clearTimeout(timer);
        resolve(result);
      });
    });
    const { publicKey } = await request(config.keyUrl);
    const encoded = publicKey.replace(/-/g, '+').replace(/_/g, '/');
    const key = Uint8Array.from(atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=')), c => c.charCodeAt(0));
    const subscription = await active.pushManager.getSubscription();
    prepared = { manager: active.pushManager, key, subscription };
    return subscription;
  }

  async function sync(state, replace) {
    let { subscription } = state;
    const currentKey = subscription?.options?.applicationServerKey;
    const mismatched = currentKey && (
      currentKey.byteLength !== state.key.length
      || !new Uint8Array(currentKey).every((byte, index) => byte === state.key[index])
    );
    if (subscription && (replace || mismatched)) {
      previousEndpoint = subscription.endpoint;
      try { localStorage.setItem(previousEndpointKey, previousEndpoint); } catch (_) {}
      if (!await subscription.unsubscribe() && await state.manager.getSubscription()) {
        throw new Error('CCM could not replace the browser subscription. Try again.');
      }
      subscription = null;
      state.subscription = null;
    }
    if (!subscription) {
      subscription = await state.manager.subscribe({ userVisibleOnly: true, applicationServerKey: state.key });
      state.subscription = subscription;
    }
    try { previousEndpoint ||= localStorage.getItem(previousEndpointKey); } catch (_) {}
    try {
      await request(config.subscribeUrl, { subscription: subscription.toJSON(), previousEndpoint });
    } catch (error) {
      if (error.status === 410 && !replace) return sync(state, true);
      throw error;
    }
    previousEndpoint = null;
    try { localStorage.removeItem(previousEndpointKey); } catch (_) {}
    return subscription;
  }

  function synchronize({ interactive = false, replace = false } = {}) {
    if (pending) return pending;
    if (interactive && prepared) {
      // Call subscribe from the button's gesture when permission is still undecided.
      pending = sync(prepared, replace);
    } else {
      pending = prepare().then(() => sync(prepared, replace));
    }
    pending = pending.finally(() => { pending = null; });
    return pending;
  }

  window.ccmPush = {
    supported,
    prepare,
    synchronize,
    status: endpoint => request(config.statusUrl, { endpoint }),
    test: endpoint => request(config.testUrl, { endpoint })
  };

  function reconcile() {
    if (supported && Notification.permission === 'granted' && navigator.onLine) {
      synchronize().catch(error => console.warn('CCM notification registration failed', error));
    }
  }

  reconcile();
  window.addEventListener('online', reconcile);
  window.addEventListener('pageshow', event => { if (event.persisted) reconcile(); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reconcile();
  });
})();
