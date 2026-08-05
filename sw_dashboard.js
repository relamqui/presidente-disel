const CACHE_NAME = 'wp-crm-dashboard-v1';
const ASSETS = [
  '/',
  '/css/dashboard.css',
  '/js/dashboard.js',
  '/img/icon-192.png',
  '/img/icon-512.png'
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.url.includes('/api/')) return;

  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
