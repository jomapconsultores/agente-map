/* ------------------------------------------------------------
 * Desarrollado por Marco Antonio Posligua San Martín
 * ------------------------------------------------------------ */

/* Service worker de Proyectos MAP.
 *
 * ── Por qué se reescribió ────────────────────────────────────────────────
 * La versión anterior guardaba en el disco del dispositivo TODA respuesta GET
 * que no estuviera en una lista de cuatro exclusiones. Esa lista dejaba fuera,
 * entre otros, `/users`, `/clientes`, `/prospectos`, `/proveedores`,
 * `/oficios` y —lo más delicado— los documentos generados
 * (`/oficios/{id}/word`, `/oficios/{id}/pdf`). Todo eso quedaba almacenado sin
 * caducidad y sobrevivía al cierre de sesión: en un equipo compartido la
 * siguiente persona podía recuperarlo.
 *
 * Ahora la regla se invierte. En vez de enumerar lo que NO se guarda —lista que
 * envejece mal, porque cada endpoint nuevo entra cacheado por omisión—, se
 * enumera lo único que SÍ se guarda: el armazón de la aplicación y sus archivos
 * estáticos. Nada de eso contiene datos de nadie.
 */

const VERSION = 'map-pwa-v2';
const CACHE = `${VERSION}-armazon`;

/* El armazón: la página vacía de la aplicación y sus recursos. Los datos los
   pide después el navegador contra la API, y esos nunca se guardan. */
const ARMAZON = ['/', '/manifest.json', '/icon.svg'];

/* Lo único que puede guardarse además del armazón. */
const ESTATICO = /\.(?:css|js|mjs|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf)$/i;

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // Uno a uno: que falte un recurso no debe impedir la instalación entera.
      .then((c) => Promise.all(ARMAZON.map((u) => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      // Se borran las versiones anteriores, incluida `map-pwa-v1`, que es donde
      // quedaron guardados los datos de clientes y los documentos generados.
      .then((ks) => Promise.all(ks.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

/* La aplicación puede pedir que se vacíe todo al cerrar sesión. */
self.addEventListener('message', (e) => {
  if (e.data && e.data.tipo === 'LIMPIAR') {
    e.waitUntil(caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k)))));
  }
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  const esNavegacion = req.mode === 'navigate';
  const esArmazon = ARMAZON.includes(url.pathname);
  const esEstatico = ESTATICO.test(url.pathname);

  /* Todo lo que no sea armazón ni estático —es decir, los datos— va a la red y
     no se toca. Sin conexión falla, que es lo correcto: más vale un error
     visible que una lista de clientes de hace tres semanas. */
  if (!esNavegacion && !esArmazon && !esEstatico) return;

  /* Navegación: red primero para que un despliegue nuevo se vea al instante;
     sin conexión, el armazón guardado. */
  if (esNavegacion) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && res.type === 'basic') {
            const copia = res.clone();
            caches.open(CACHE).then((c) => c.put('/', copia));
          }
          return res;
        })
        .catch(() => caches.match('/')),
    );
    return;
  }

  /* Armazón y estáticos: caché primero, que es donde esto rinde. */
  e.respondWith(
    caches.match(req).then((cacheado) => cacheado || fetch(req).then((res) => {
      if (res.ok && res.type === 'basic') {
        const copia = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copia));
      }
      return res;
    })),
  );
});
