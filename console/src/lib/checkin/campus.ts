"use client";

import { api } from "@/lib/api";

/**
 * Which campus this check-in is for.
 *
 * Used to silently resolve to the school's primary campus via
 * `GET /attendance/qr-token` and cache that forever -- a staff member at a
 * second campus had no way to correct it and no indication it was even
 * wrong (see `CampusListView` in `educore/presence/views.py` for the fuller
 * account). `GET /attendance/campuses` now lets the person choose, and the
 * choice is shown, not just assumed.
 *
 * The cached value is a convenience default for next time, not a lock --
 * `resolveCampusId` still validates it against the real campus list, so a
 * campus that no longer exists (renamed school, merged site) never gets
 * silently reused. Cached in localStorage (not IndexedDB -- small,
 * non-sensitive, synchronous-enough) so a fully offline device can still
 * stamp a campus on a queued check-in without a round trip.
 */

export type Campus = { id: string; name: string; is_primary: boolean };

const STORAGE_KEY = "educore.checkin.campus_id";

export function cachedCampusId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(STORAGE_KEY);
}

export function setCachedCampusId(campusId: string) {
  window.localStorage.setItem(STORAGE_KEY, campusId);
}

export async function listCampuses(): Promise<Campus[]> {
  const response = await api.get<{ results: Campus[] }>("/attendance/campuses");
  return response.results;
}

/** Resolves to a campus id known to be real and picked, not just cached. */
export async function resolveCampusId(): Promise<{
  campusId: string;
  campuses: Campus[];
}> {
  const campuses = await listCampuses();
  if (campuses.length === 0) {
    throw new Error("This school has no campus set up yet.");
  }

  const cached = cachedCampusId();
  const stillReal = cached && campuses.some((c) => c.id === cached);
  const campusId = stillReal
    ? cached!
    : (campuses.find((c) => c.is_primary) ?? campuses[0]).id;

  setCachedCampusId(campusId);
  return { campusId, campuses };
}
