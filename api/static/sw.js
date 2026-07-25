/* Desarrollado por Marco Antonio Posligua San Martín */
/* Service Worker — Proyectos MAP */
const CACHE = 'map-pwa-v1';
const STATIC = ['/', '/manifest.json', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(ks =>
      Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  /* API siempre en red */
  if (e.request.method !== 'GET') return;
  if (url.includes('/auth/') || url.includes('/propuestas') || url.includes('/buscar') || url.includes('/generar')) return;

  /* Estrategia: red primero, caché como fallback (para la shell offline) */
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});
