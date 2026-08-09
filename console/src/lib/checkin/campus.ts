"use client";

import { api } from "@/lib/api";

/**
 * Which campus this check-in is for.
 *
 * There is no "list my campuses" endpoint in the built API -- presence only
 * exposes `GET /attendance/qr-token`, which resolves to the caller's primary
 * campus when no `campus_id` is given and returns that id in its response.
 * `qr.issue` is a pure signing call with no database write (see
 * `educore/core/signed_tokens.py`), so calling it just to read `campus_id`
 * off the response has no side effect worth avoiding.
 *
 * Cached in localStorage (not IndexedDB -- this is a small, non-sensitive,
 * synchronous-enough value, not a queued fact) so a fully offline device can
 * still stamp a campus on a queued check-in without a round trip.
 */

const STORAGE_KEY = "educore.checkin.campus_id";

export function cachedCampusId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(STORAGE_KEY);
}

function setCachedCampusId(campusId: string) {
  window.localStorage.setItem(STORAGE_KEY, campusId);
}

export async function resolveCampusId(): Promise<string> {
  const cached = cachedCampusId();
  if (cached) return cached;

  const response = await api.get<{ campus_id: string }>(
    "/attendance/qr-token",
  );
  setCachedCampusId(response.campus_id);
  return response.campus_id;
}
