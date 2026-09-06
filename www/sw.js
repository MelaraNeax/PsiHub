/**
 * PsyHub — Service Worker
 * Cache para shell de la app. La búsqueda siempre va a la red.
 */

const CACHE = 'psyhub-v2';
const SHELL = [
    './',
    './index.html',
    './style.css',
    './app.js',
    './manifest.json',
    './src/services/openalex.js'
];

// Instalar: pre-cachear la shell
self.addEventListener('install', event => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(CACHE).then(cache => cache.addAll(SHELL))
    );
});

// Activar: limpiar caches viejos
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

// Fetch: Shell desde caché, OpenAlex siempre desde red
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // Siempre a la red para OpenAlex
    if (url.hostname === 'api.openalex.org') {
        event.respondWith(fetch(event.request));
        return;
    }

    // Fuentes de Google → network first, fallback caché
    if (url.hostname.includes('fonts.')) {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(event.request))
        );
        return;
    }

    // Shell de la app → caché first, fallback red
    event.respondWith(
        caches.match(event.request).then(cached => cached || fetch(event.request))
    );
});
