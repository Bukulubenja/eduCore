"use client";

import type { Signals } from "./types";

/**
 * One geofence reading, shaped for `evaluate_geofence` in
 * `educore/presence/evaluators.py`: `lat`, `lon`, `accuracy_m`.
 *
 * A denied permission or a timed-out fix is not an error to surface loudly --
 * geofence is one signal among several and the evaluator treats a missing one
 * as `unavailable`, not a failure (that distinction is the whole point of
 * ADR-0002). Callers get `null` and carry on without it.
 */
export async function captureGeofence(): Promise<Signals["geofence"] | null> {
  if (typeof navigator === "undefined" || !navigator.geolocation) return null;

  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          accuracy_m: Math.round(position.coords.accuracy),
        });
      },
      () => resolve(null),
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 15000 },
    );
  });
}
