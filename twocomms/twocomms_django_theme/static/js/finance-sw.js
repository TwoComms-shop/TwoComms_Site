/* TwoComms Finance — Service Worker для PWA
   Кешування критичних ресурсів, offline підтримка, push-повідомлення */

const CACHE_VERSION = 'twc-finance-v1.5.0';
const CACHE_STATIC = `${CACHE_VERSION}-static`;
const CACHE_DYNAMIC = `${CACHE_VERSION}-dynamic`;
const CACHE_API = `${CACHE_VERSION}-api`;

// Критичні ресурси для offline роботи.
// УВАГА: НЕ кешуємо тут CSS/JS за нехешованими шляхами (вони віддаються через
// ManifestStaticFilesStorage із хешем у назві). Інакше cache-first віддавав би
// застарілі стилі/скрипти після деплою. Тримаємо лише оболонку та шрифти.
const STATIC_ASSETS = [
  '/',
  '/static/img/logo.svg',
  '/static/finance-manifest.json',
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Orbitron:wght@500;700&display=swap'
];

// Максимальний розмір кешу
const MAX_CACHE_SIZE = 50;
const MAX_CACHE_AGE = 7 * 24 * 60 * 60 * 1000; // 7 днів

// ===== Встановлення Service Worker =====
self.addEventListener('install', (event) => {
  console.log('[Finance SW] Installing...');

  event.waitUntil(
    caches.open(CACHE_STATIC)
      .then((cache) => {
        console.log('[Finance SW] Caching static assets');
        return cache.addAll(STATIC_ASSETS.map(url => new Request(url, { cache: 'reload' })));
      })
      .then(() => self.skipWaiting())
      .catch((err) => {
        console.error('[Finance SW] Install failed:', err);
      })
  );
});

// ===== Активація Service Worker =====
self.addEventListener('activate', (event) => {
  console.log('[Finance SW] Activating...');

  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {
        return Promise.all(
          cacheNames
            .filter((name) => name.startsWith('twc-finance-') && name !== CACHE_STATIC && name !== CACHE_DYNAMIC && name !== CACHE_API)
            .map((name) => {
              console.log('[Finance SW] Deleting old cache:', name);
              return caches.delete(name);
            })
        );
      })
      .then(() => self.clients.claim())
  );
});

// ===== Обробка запитів =====
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Ігноруємо non-GET запити
  if (request.method !== 'GET') {
    return;
  }

  // Ігноруємо chrome extensions та інші протоколи
  if (!url.protocol.startsWith('http')) {
    return;
  }

  // API запити: Network First з fallback на cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstStrategy(request, CACHE_API));
    return;
  }

  // Статичні ресурси: Cache First з network fallback
  if (isStaticAsset(url)) {
    event.respondWith(cacheFirstStrategy(request, CACHE_STATIC));
    return;
  }

  // HTML сторінки: Network First з cache fallback
  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(networkFirstStrategy(request, CACHE_DYNAMIC));
    return;
  }

  // Інші ресурси: Cache First
  event.respondWith(cacheFirstStrategy(request, CACHE_DYNAMIC));
});

// ===== Стратегія: Cache First =====
async function cacheFirstStrategy(request, cacheName) {
  try {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(request);

    if (cached) {
      // Повертаємо з кешу, але оновлюємо в фоні
      updateCache(request, cache);
      return cached;
    }

    // Немає в кеші - завантажуємо з мережі
    const response = await fetch(request);

    if (response.ok) {
      cache.put(request, response.clone());
    }

    return response;
  } catch (error) {
    console.error('[Finance SW] Cache first failed:', error);

    // Fallback для HTML - показуємо offline сторінку
    if (request.headers.get('accept')?.includes('text/html')) {
      const cache = await caches.open(CACHE_STATIC);
      return cache.match('/static/offline.html') || new Response('Offline', { status: 503 });
    }

    throw error;
  }
}

// ===== Стратегія: Network First =====
async function networkFirstStrategy(request, cacheName) {
  try {
    const response = await fetch(request);

    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
      trimCache(cacheName, MAX_CACHE_SIZE);
    }

    return response;
  } catch (error) {
    console.log('[Finance SW] Network failed, trying cache:', request.url);

    const cache = await caches.open(cacheName);
    const cached = await cache.match(request);

    if (cached) {
      return cached;
    }

    // Fallback для HTML
    if (request.headers.get('accept')?.includes('text/html')) {
      const staticCache = await caches.open(CACHE_STATIC);
      return staticCache.match('/static/offline.html') || new Response('Offline', { status: 503 });
    }

    throw error;
  }
}

// ===== Оновлення кешу в фоні =====
async function updateCache(request, cache) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
  } catch (error) {
    // Ігноруємо помилки фонового оновлення
  }
}

// ===== Обмеження розміру кешу =====
async function trimCache(cacheName, maxSize) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();

  if (keys.length > maxSize) {
    await cache.delete(keys[0]);
    trimCache(cacheName, maxSize);
  }
}

// ===== Перевірка чи це статичний ресурс =====
function isStaticAsset(url) {
  const staticExtensions = ['.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff', '.woff2', '.ttf'];
  return staticExtensions.some(ext => url.pathname.endsWith(ext)) || url.pathname.includes('/static/');
}

// ===== Push-повідомлення =====
self.addEventListener('push', (event) => {
  console.log('[Finance SW] Push received');

  let data = { title: 'TwoComms Finance', body: 'Нове повідомлення' };

  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: '/static/img/favicon-192x192.png',
    badge: '/static/img/favicon-192x192.png',
    vibrate: [200, 100, 200],
    tag: data.tag || 'finance-notification',
    requireInteraction: data.requireInteraction || false,
    data: {
      url: data.url || '/',
      fallbackUrl: data.fallback_url || data.url || '/',
      reportId: data.report_id || null,
      notificationId: data.notification_id || data.report_id || null,
      notificationType: data.notification_type || '',
      kind: data.kind || '',
      transactionId: data.transaction_id || null,
      reviewId: data.review_id || null,
      suggestedKind: data.suggested_kind || '',
      actionUrls: data.action_urls || {},
      timestamp: Date.now()
    },
    actions: data.actions || []
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// ===== Клік по повідомленню =====
self.addEventListener('notificationclick', (event) => {
  console.log('[Finance SW] Notification clicked, action:', event.action);

  const data = event.notification.data || {};
  const reportId = data.reportId || data.report_id || null;
  const action = event.action || 'open';
  event.notification.close();

  // Дія «Ознайомився» — позначаємо лог і не відкриваємо вікно.
  if (event.action === 'ack' && reportId) {
    event.waitUntil(
      fetch('/api/notifications/' + reportId + '/ack/', {
        method: 'POST',
        credentials: 'include'
      }).catch(() => {})
    );
    return;
  }

  const urlToOpen = (data.actionUrls && data.actionUrls[action]) || data.url || data.fallbackUrl || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        // Якщо Finance уже відкрита, передаємо контекст операції без 404 і
        // без повного перезавантаження. Це також працює в iOS PWA fallback.
        for (const client of clientList) {
          if (client.url.indexOf(self.location.origin) === 0 && 'focus' in client) {
            client.focus();
            if (data.transactionId) {
              client.postMessage({
                type: 'OPEN_TRANSACTION',
                transactionId: data.transactionId,
                notificationId: data.notificationId || reportId,
                action,
              });
            } else if (reportId) {
              client.postMessage({ type: 'OPEN_REPORT', reportId: reportId });
            }
            return;
          }
        }
        // Інакше відкриваємо нову вкладку (URL уже містить ?fin_report=<id>).
        if (clients.openWindow) {
          return clients.openWindow(urlToOpen);
        }
      })
  );
});

// ===== Синхронізація в фоні =====
self.addEventListener('sync', (event) => {
  console.log('[Finance SW] Background sync:', event.tag);

  if (event.tag === 'sync-transactions') {
    event.waitUntil(syncTransactions());
  }
});

async function syncTransactions() {
  try {
    // Тут буде логіка синхронізації транзакцій
    console.log('[Finance SW] Syncing transactions...');
  } catch (error) {
    console.error('[Finance SW] Sync failed:', error);
    throw error;
  }
}

// ===== Повідомлення від клієнта =====
self.addEventListener('message', (event) => {
  console.log('[Finance SW] Message received:', event.data);

  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data.type === 'CLEAR_CACHE') {
    event.waitUntil(
      caches.keys().then((cacheNames) => {
        return Promise.all(
          cacheNames
            .filter((name) => name.startsWith('twc-finance-'))
            .map((name) => caches.delete(name))
        );
      })
    );
  }
});
