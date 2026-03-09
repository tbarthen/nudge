const CACHE_NAME = 'nudge-v20';
const ASSETS = [
  '/',
  '/static/manifest.json',
];

// Install — cache shell assets
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Push — show notification from server
self.addEventListener('push', e => {
  let data = { title: 'Nudge — DO IT!!!', body: 'You have reminders waiting.', tag: 'nudge-reminder' };
  try { data = e.data.json(); } catch (_) {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icon-192.png',
      tag: data.tag || 'nudge-reminder',
      renotify: true,
    })
  );
});

// Notification click — focus or open the app
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      if (clients.length > 0) {
        return clients[0].focus();
      }
      return self.clients.openWindow('/');
    })
  );
});

// Fetch — network-first for API, cache-first for assets
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API calls: always go to network (offline handled by IndexedDB in app)
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // Everything else: network-first with cache fallback
  e.respondWith(
    fetch(e.request)
      .then(res => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || new Response('Offline', { status: 503, statusText: 'Offline' })))
  );
});
