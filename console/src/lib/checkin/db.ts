"use client";

import type { QueuedOperation } from "./types";

/**
 * The offline outbox.
 *
 * IndexedDB, not localStorage: a queued check-in is a fact someone is relying
 * on ("it went through") and has to survive a reload, a crashed tab, and a
 * background-sync event firing with no page open at all. localStorage is
 * synchronous, capped at a few MB, and invisible to a service worker running
 * without a document -- none of which this can tolerate.
 *
 * `public/sw.js` opens this same database by name/store/keyPath to flush the
 * queue from a Background Sync event when no tab is open. Keep the two in
 * sync if this schema changes.
 */

export const DB_NAME = "educore-checkin";
export const DB_VERSION = 1;
export const STORE_NAME = "outbox";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB is not available in this browser."));
      return;
    }

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

export async function enqueue(operation: QueuedOperation): Promise<void> {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put(operation);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}

export async function listQueued(): Promise<QueuedOperation[]> {
  const db = await openDb();
  const result = await new Promise<QueuedOperation[]>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const request = tx.objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve(request.result as QueuedOperation[]);
    request.onerror = () => reject(request.error);
  });
  db.close();
  // Oldest first: a batch should replay in the order it happened, so a
  // check-out never lands ahead of the check-in it followed.
  return result.sort((a, b) => a.queued_at.localeCompare(b.queued_at));
}

export async function countQueued(): Promise<number> {
  const db = await openDb();
  const count = await new Promise<number>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const request = tx.objectStore(STORE_NAME).count();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  db.close();
  return count;
}

export async function remove(clientEventId: string): Promise<void> {
  const db = await openDb();
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).delete(clientEventId);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
  db.close();
}
