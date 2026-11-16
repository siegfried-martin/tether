// Tether service worker: caches the app shell only, never API responses.
const CACHE = 'tether-shell-v2'
const NEVER = ['/api/', '/auth/', '/accounts/']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/', '/manifest.webmanifest', '/icon.svg'])))
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET' || url.origin !== location.origin) return
  if (NEVER.some((p) => url.pathname.startsWith(p))) return

  if (event.request.mode === 'navigate') {
    // Network first for pages, so a deploy shows up on the next load; shell from cache offline.
    // Only the app shell is stored under '/'; other pages (e.g. /privacy.html) are cached as themselves.
    const key = url.pathname.endsWith('.html') ? event.request : '/'
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          // The public home page is served at / when signed out; it must not become the cached app shell.
          if (resp.ok && !(key === '/' && resp.headers.get('X-Tether-Page') === 'home')) {
            const copy = resp.clone()
            caches.open(CACHE).then((c) => c.put(key, copy))
          }
          return resp
        })
        .catch(() => caches.match(key).then((hit) => hit || caches.match('/'))),
    )
    return
  }
  if (url.pathname.startsWith('/assets/')) {
    // Hashed build assets never change: cache first.
    event.respondWith(
      caches.match(event.request).then(
        (hit) =>
          hit ||
          fetch(event.request).then((resp) => {
            const copy = resp.clone()
            caches.open(CACHE).then((c) => c.put(event.request, copy))
            return resp
          }),
      ),
    )
  }
})
