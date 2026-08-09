/**
 * Service worker for the staff check-in PWA.
 *
 * Hand-written rather than generated: this Next.js version (16.3.0) has no
 * built-in service-worker bundling story (see the "Extending your PWA"
 * section of the framework's own Progressive Web Apps guide), and adding
 * next-pwa/Serwist for two responsibilities -- cache the check-in shell,
 * replay a small IndexedDB queue -- would be a new dependency to do less than
 * this file does unassisted. Registered from `ServiceWorkerBoot`
 * (`src/components/checkin/service-worker-boot.tsx`), scoped to `/checkin/`
 * only: it has no business caching or intercepting the leadership dashboard.
 *
 * Two jobs:
 *   1. Cache the check-in app shell so the route still loads with no network.
 *   2. On a Background Sync event, flush the offline outbox even if the tab
 *      that queued it has since closed.
 *
 * This duplicates small pieces of `src/lib/checkin/db.ts` and
 * `src/lib/checkin/submit.ts` (the IndexedDB schema and the /sync flush) by
 * necessity -- a service worker loads as a plain script, not through the
 * app's bundler, so it cannot import those TypeScript modules. Keep the
 * `DB_NAME`/`DB_VERSION`/`STORE_NAME` constants and the /sync request shape
 * in step with those files if either changes.
 */

const CACHE_NAME = "educore-checkin-v1";
const APP_SHELL = ["/checkin", "/checkin/history", "/manifest.webmanifest"];

const DB_NAME = "educore-checkin";
const DB_VERSION = 1;
const STORE_NAME = "outbox";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      // Take over immediately: a teacher who just installed the app should
      // not have to close and reopen it for offline support to apply.
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // API calls (including the offline queue's own POSTs) pass straight
  // through: caching someone's attendance record and quietly serving it
  // stale would be worse than the request simply failing, since the client
  // already has an explicit queued/offline state for that case.
  if (url.pathname.startsWith("/api/")) return;
  if (!url.pathname.startsWith("/checkin")) return;

  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached || caches.match("/checkin"));

      // Stale-while-revalidate: serve the cached shell instantly if we have
      // one, refresh it in the background. A cold cache falls through to the
      // network (or the offline fallback above) instead.
      return cached || network;
    }),
  );
});

self.addEventListener("sync", (event) => {
  if (event.tag === "flush-checkin-queue") {
    event.waitUntil(flushQueue());
  }
});

// A page can also ask directly (e.g. right after regaining focus), without
// waiting on the platform's own Background Sync scheduling.
self.addEventListener("message", (event) => {
  if (event.data?.type === "flush-checkin-queue") {
    event.waitUntil(flushQueue());
  }
});

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "client_event_id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function listQueued() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const request = tx.objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function removeQueued(clientEventId) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).delete(clientEventId);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function flushQueue() {
  const pending = await listQueued();
  if (pending.length === 0) return;

  let response;
  try {
    response = await fetch("/api/proxy/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_time: new Date().toISOString(),
        operations: pending,
      }),
    });
  } catch {
    // Still offline. Background Sync retries this automatically with
    // platform-managed backoff; nothing to do here but leave the queue be.
    return;
  }

  if (!response.ok) return;

  const body = await response.json().catch(() => null);
  for (const result of body?.results ?? []) {
    await removeQueued(result.client_event_id);
  }

  const clients = await self.clients.matchAll();
  for (const client of clients) {
    client.postMessage({ type: "checkin-queue-flushed" });
  }
}
