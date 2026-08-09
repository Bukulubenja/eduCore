/**
 * Shapes shared between the check-in UI, the offline queue, and the service
 * worker's own (duplicated, plain-JS) copy of the flush logic in
 * `public/sw.js`. Keep both in sync if this changes.
 */

export type EventKind = "check_in" | "check_out";

export type OperationType = "attendance.check_in" | "attendance.check_out";

/** Device-reported evidence. Keyed by signal type, per `RawSignalsField`
 * in `educore/presence/serializers.py` -- nothing here is trusted server-side,
 * it is validated for shape only and scored by the evaluators. */
export type Signals = {
  qr?: { token: string };
  geofence?: { lat: number; lon: number; accuracy_m: number };
};

export type CheckPayload = {
  campus_id: string;
  signals: Signals;
};

/** One offline-queued operation, stored in IndexedDB keyed by
 * `client_event_id` -- the same idempotency key the server expects, so a
 * queued item that raced a direct submission is absorbed for free rather
 * than double-counted. */
export type QueuedOperation = {
  client_event_id: string;
  type: OperationType;
  captured_at: string;
  payload: CheckPayload;
  queued_at: string;
};

export type Disposition = "verified" | "provisional" | "rejected";

export type SignalResult = {
  signal_type: string;
  verdict: "pass" | "fail" | "unavailable";
  weight_applied: number;
  hard_fail: boolean;
  detail: string;
  payload: Record<string, unknown>;
};

export type AttendanceEventResource = {
  id: string;
  kind: EventKind;
  captured_at: string;
  received_at: string;
  clock_skew_seconds: number;
  disposition: Disposition;
  confidence: number;
  rejection_reason: string;
  policy_version: number;
  signals: SignalResult[];
};

export type AttendanceRecord = {
  id: string;
  membership_id: string;
  date: string;
  status: "present" | "late" | "partial" | "absent" | "on_leave" | "holiday";
  disposition: Disposition;
  confidence: number;
  first_in_at: string | null;
  last_out_at: string | null;
  minutes_on_site: number;
  resolution: string;
  policy_version: number;
  needs_review: boolean;
};

export type CheckInResponse = {
  event: AttendanceEventResource;
  record: AttendanceRecord;
  server_time: string;
  clock_skew_seconds: number;
};

/** The state the UI actually needs to show. Collapses the server's
 * accepted/provisional/rejected split with the client's own "no network yet"
 * state -- three real outcomes, not an error path pretending to be one. */
export type SubmissionOutcome =
  | { kind: "accepted"; disposition: Disposition; confidence: number }
  | { kind: "queued" }
  | { kind: "rejected"; reason: string };
