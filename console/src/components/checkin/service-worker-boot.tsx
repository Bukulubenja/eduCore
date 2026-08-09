"use client";

import { useEffect } from "react";

/**
 * Registers `public/sw.js`, scoped to `/checkin/` only.
 *
 * A component rather than inline in the layout so it can stay a client
 * boundary by itself; it renders nothing.
 */
export function ServiceWorkerBoot() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker
      .register("/sw.js", { scope: "/checkin/" })
      .catch(() => {
        // No offline asset caching or background sync this session; the
        // in-page online-event flush (useOfflineQueue) still covers syncing.
      });
  }, []);

  return null;
}
