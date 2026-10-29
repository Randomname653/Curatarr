// ── Service worker: installable app + an app shell that opens offline ────────
// Scope is the whole origin (served from /sw.js). Only the static shell is
// cached: /api/* never touches the cache — answers there are per-user, carry
// a bearer token, and a stale one would show the owner a library that no
// longer exists. The shell is network-first (the server sends js/ and css/
// as no-cache, and a release must show up on the next load, not the one
// after); the cache is the fallback for when the server is down or the phone
// has no route home. Bump VERSION to drop an old shell on activate.
const VERSION = 'curatarr-shell-v2';   // v2: the new logo
const SHELL = [
  '/',
  '/css/app.css',
  '/vendor/marked.min.js',
  '/vendor/purify.min.js',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/favicon.svg',
  '/offline.html',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(VERSION)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

function _isShell(url) {
  return url.origin === self.location.origin
    && !url.pathname.startsWith('/api')
    && url.pathname !== '/sw.js';
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (!_isShell(url)) return;          // API, posters, SSE: straight to the network

  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put(req, copy)).catch(() => {});
      }
      return res;
    } catch (err) {
      const cached = await caches.match(req, {ignoreSearch: true});
      if (cached) return cached;
      if (req.mode === 'navigate') {
        return (await caches.match('/')) || (await caches.match('/offline.html')) || Response.error();
      }
      return Response.error();
    }
  })());
});
