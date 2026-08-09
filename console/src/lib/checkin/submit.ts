"use client";

import * as queue from "./db";
import type {
  CheckInResponse,
  CheckPayload,
  EventKind,
  OperationType,
  QueuedOperation,
  SubmissionOutcome,
} from "./types";

/**
 * Submit one check-in/check-out.
 *
 * Tries the direct endpoint first (`POST /attendance/check-in|check-out`)
 * because it answers immediately with the real disposition -- accepted or
 * provisional -- which a batched `/sync` response also carries, but there is
 * no reason to make someone wait for a background flush when the network is
 * right there.
 *
 * Only a genuine network failure (the `fetch` itself rejecting -- offline, DNS
 * down, request aborted) falls back to the offline queue. An HTTP error
 * response is a real, considered answer from the server -- 422 "not
 * acceptable", 409 "duplicate" -- and queuing it would just replay the same
 * rejection later while hiding it from the person right now. That distinction
 * is why this does not simply wrap `api.post` and catch everything.
 */
export async function submitCheck(
  kind: EventKind,
  payload: CheckPayload,
): Promise<SubmissionOutcome> {
  const clientEventId = crypto.randomUUID();
  const capturedAt = new Date().toISOString();
  const path = kind === "check_in" ? "/attendance/check-in" : "/attendance/check-out";

  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    await queueOperation(kind, clientEventId, capturedAt, payload);
    return { kind: "queued" };
  }

  try {
    const response = await fetch(`/api/proxy${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_event_id: clientEventId,
        campus_id: payload.campus_id,
        captured_at: capturedAt,
        signals: payload.signals,
      }),
    });

    const body = await response.json().catch(() => null);

    if (!response.ok) {
      if (response.status === 401) {
        window.location.href = "/login";
      }
      return {
        kind: "rejected",
        reason: body?.detail ?? "The server could not accept this check-in.",
      };
    }

    const data = body as CheckInResponse;
    return {
      kind: "accepted",
      disposition: data.event.disposition,
      confidence: data.event.confidence,
    };
  } catch {
    // fetch() only rejects on a network-level failure -- exactly the case
    // this queue exists for.
    await queueOperation(kind, clientEventId, capturedAt, payload);
    return { kind: "queued" };
  }
}

async function queueOperation(
  kind: EventKind,
  clientEventId: string,
  capturedAt: string,
  payload: CheckPayload,
) {
  const operation: QueuedOperation = {
    client_event_id: clientEventId,
    type: (kind === "check_in"
      ? "attendance.check_in"
      : "attendance.check_out") as OperationType,
    captured_at: capturedAt,
    payload,
    queued_at: new Date().toISOString(),
  };
  await queue.enqueue(operation);
  await registerBackgroundSync();
}

/** Best-effort: ask the platform to wake the service worker and flush the
 * queue even if this tab closes before connectivity returns. Chromium-family
 * browsers only -- everywhere else, the `online` listener registered by
 * `useQueueFlush` is what actually does the work, so this is an enhancement,
 * not a dependency. */
async function registerBackgroundSync() {
  if (!("serviceWorker" in navigator) || !("SyncManager" in window)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    // @ts-expect-error -- SyncManager is not in lib.dom.d.ts yet.
    await registration.sync.register("flush-checkin-queue");
  } catch {
    // Not fatal: the online-event flush below still covers it.
  }
}

export type FlushResult = {
  attempted: number;
  accepted: number;
  rejected: number;
};

/** Replay everything queued through the batched sync endpoint. Per-operation
 * results (ADR-0003): one bad record in the batch never blocks the rest, and
 * a result of "accepted", "duplicate", or "rejected" is equally final --
 * all three mean the server has definitively judged that operation, so it
 * comes off the local queue either way. Only a network failure during the
 * flush itself leaves the queue untouched for the next attempt. */
export async function flushQueue(): Promise<FlushResult> {
  const pending = await queue.listQueued();
  if (pending.length === 0) return { attempted: 0, accepted: 0, rejected: 0 };

  let response: Response;
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
    return { attempted: 0, accepted: 0, rejected: 0 };
  }

  if (!response.ok) {
    // A rejected batch (e.g. session expired) leaves the queue intact for
    // the next trigger rather than discarding evidence of what happened.
    return { attempted: 0, accepted: 0, rejected: 0 };
  }

  const body = (await response.json().catch(() => null)) as {
    results?: { client_event_id: string; status: string }[];
  } | null;

  let accepted = 0;
  let rejected = 0;
  for (const result of body?.results ?? []) {
    await queue.remove(result.client_event_id);
    if (result.status === "rejected") rejected += 1;
    else accepted += 1;
  }

  return { attempted: pending.length, accepted, rejected };
}

export const queuedCount = queue.countQueued;
