// ── BARO Service Worker ───────────────────────
// Stratégie : Cache-first pour assets statiques,
//             Network-first pour l'API

const CACHE_NAME = 'baro-v303-ia';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/app.js',
  '/styles.css',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

// ── Installation : mise en cache des assets ───
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// ── Activation : nettoyage des anciens caches ─
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(k => k !== CACHE_NAME)
          .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch : stratégie hybride ─────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // CDN (Chart.js, jsPDF) → network avec fallback cache
  if (url.hostname.includes('cdn.jsdelivr.net') ||
      url.hostname.includes('cdnjs.cloudflare.com')) {
    event.respondWith(
      fetch(event.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Requêtes cross-origin (backend API externe) → ne pas intercepter
  // Laisse le navigateur gérer directement pour que le fallback localStorage fonctionne
  if (url.origin !== self.location.origin) {
    return;
  }

  // API same-origin → network-first, erreur JSON si hors-ligne
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(
          JSON.stringify({ error: 'Hors-ligne — données locales utilisées' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        )
      )
    );
    return;
  }

  // HTML navigations (index.html, /) → network-first pour garantir la dernière version
  // (sinon le browser cache peut servir un index.html qui référence d'anciens ?v=)
  const isHTMLNav = event.request.mode === 'navigate' ||
                    (event.request.destination === 'document') ||
                    url.pathname === '/' ||
                    url.pathname.endsWith('/') ||
                    url.pathname.endsWith('index.html');
  if (isHTMLNav) {
    event.respondWith(
      fetch(event.request)
        .then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(event.request).then(r => r || caches.match('/index.html')))
    );
    return;
  }

  // Assets statiques → cache-first
  event.respondWith(
    caches.match(event.request)
      .then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return res;
        });
      })
      .catch(() => caches.match('/index.html'))
  );
});

// ── Message : forcer la mise à jour du cache ──
self.addEventListener('message', event => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});

// ── Push : notification de nouvelle commande (serveur → appareil) ──
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(data.title || 'BARO', {
      body: data.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      vibrate: [80, 40, 80],
      tag: 'baro-order',
      renotify: true,
      data: { url: data.url || '/' },
    })
  );
});

// ── Clic sur la notification : ouvre / ramène l'app ──
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) { if ('focus' in c) return c.focus(); }
      return clients.openWindow(url);
    })
  );
});
