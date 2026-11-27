// Tether service worker: caches the app shell only, never API responses, and shows notifications.
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

// Notifications. The server sends {title, body, tag, url, ts}, encrypted for this device.
self.addEventListener('push', (event) => {
  let data = {}
  try { data = event.data ? event.data.json() : {} } catch { data = { body: event.data && event.data.text() } }
  event.waitUntil(self.registration.showNotification(data.title || 'Tether', {
    body: data.body || '',
    tag: data.tag,
    renotify: Boolean(data.tag),    // a newer message in the same thread still alerts
    timestamp: data.ts,
    icon: '/icon-192.png',
    data: { url: data.url || '/' },
  }))
})

// A tap opens the page it points at: in the open app when there is one, else a new window.
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = new URL(event.notification.data?.url || '/', self.location.origin)
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const client = open.find((c) => new URL(c.url).origin === url.origin)
    if (client) {
      client.postMessage({ type: 'open', url: url.pathname + url.search })
      return client.focus()
    }
    return self.clients.openWindow(url.href)
  })())
})

// The browser renewed this device's push subscription: give the server the new one.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription
    const sub = event.newSubscription || (old && await self.registration.pushManager.subscribe(old.options))
    if (!sub) return
    await fetch('/api/push/subscription', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...sub.toJSON(), replaces: old ? old.endpoint : null }),
    })
  })())
})
