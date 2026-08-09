"use client";

import { useCallback, useEffect, useState } from "react";

import { flushQueue, queuedCount } from "./submit";

/**
 * Keeps the pending-queue count current and flushes it whenever there is a
 * real chance of success: on mount (in case items survived a reload), when
 * the browser fires `online`, and when the service worker reports it flushed
 * the queue itself via Background Sync.
 *
 * `online` firing is not proof the API is reachable -- it only means the OS
 * network interface came up -- so `flushQueue` is written to fail quietly
 * and leave the queue intact if the follow-up request still cannot land.
 */
export function useOfflineQueue() {
  const [pending, setPending] = useState(0);
  const [syncing, setSyncing] = useState(false);

  const refresh = useCallback(() => {
    queuedCount().then(setPending).catch(() => {});
  }, []);

  const flushNow = useCallback(async () => {
    setSyncing(true);
    try {
      await flushQueue();
    } finally {
      setSyncing(false);
      refresh();
    }
  }, [refresh]);

  useEffect(() => {
    refresh();
    // Deferred to a microtask: `flushNow` sets state before its first
    // `await`, and calling it synchronously here would run that setState as
    // part of this effect's own execution, which React's linting (rightly)
    // treats as a same-render cascade risk. Queuing it breaks that chain
    // without changing when it actually runs in practice (still effectively
    // immediate on mount).
    queueMicrotask(() => {
      flushNow();
    });

    function onOnline() {
      flushNow();
    }
    function onMessage(event: MessageEvent) {
      if (event.data?.type === "checkin-queue-flushed") refresh();
    }

    window.addEventListener("online", onOnline);
    navigator.serviceWorker?.addEventListener?.("message", onMessage);
    return () => {
      window.removeEventListener("online", onOnline);
      navigator.serviceWorker?.removeEventListener?.("message", onMessage);
    };
  }, [refresh, flushNow]);

  return { pending, syncing, refresh, flushNow };
}
