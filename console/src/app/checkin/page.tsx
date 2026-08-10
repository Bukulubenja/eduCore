"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui";
import { QrCapture } from "@/components/checkin/qr-capture";
import { StatusCard } from "@/components/checkin/status-card";
import {
  cachedCampusId,
  resolveCampusId,
  setCachedCampusId,
  type Campus,
} from "@/lib/checkin/campus";
import { captureGeofence } from "@/lib/checkin/geolocation";
import { submitCheck } from "@/lib/checkin/submit";
import type { Signals, SubmissionOutcome } from "@/lib/checkin/types";
import { useOfflineQueue } from "@/lib/checkin/use-offline-queue";

/** A scanned QR token is only good for the policy's TTL (30s by default --
 * `AttendancePolicy.qr_token_ttl_seconds` in `educore/presence/models.py`).
 * Submitting it past that isn't merely useless, it is actively worse than
 * sending nothing: an expired-but-present token is a `hard_fail` verdict
 * (`evaluate_qr` in `educore/presence/evaluators.py`), which zeroes
 * confidence and rejects the whole check-in outright. 25s gives a five-second
 * safety margin for clock skew and the walk from the display back to this
 * screen. */
const QR_FRESHNESS_MS = 25_000;

type ScannedCode = { token: string; capturedAt: number };

export default function CheckinPage() {
  const [campusId, setCampusId] = useState<string | null>(cachedCampusId());
  const [campuses, setCampuses] = useState<Campus[]>([]);
  const [pickingCampus, setPickingCampus] = useState(false);
  const [campusError, setCampusError] = useState("");
  const [geo, setGeo] = useState<Signals["geofence"] | null>(null);
  const [qr, setQr] = useState<ScannedCode | null>(null);
  const [qrAgeMs, setQrAgeMs] = useState(0);
  const [busy, setBusy] = useState<"check_in" | "check_out" | null>(null);
  const [outcome, setOutcome] = useState<SubmissionOutcome | null>(null);
  const { pending, syncing, flushNow } = useOfflineQueue();

  // Resolve the campus once, and capture a location fix eagerly so it is
  // ready by the time someone actually taps a button -- waiting on GPS after
  // the tap would make the one-tap check-in feel like anything but.
  useEffect(() => {
    resolveCampusId()
      .then(({ campusId: resolved, campuses: list }) => {
        setCampusId(resolved);
        setCampuses(list);
      })
      .catch(() => {
        if (!cachedCampusId()) {
          setCampusError(
            "Couldn't reach the school to set up this device. Connect once and reopen this page.",
          );
        }
      });
    captureGeofence().then(setGeo);
  }, []);

  const campus = campuses.find((c) => c.id === campusId);

  function chooseCampus(id: string) {
    setCampusId(id);
    setCachedCampusId(id);
    setPickingCampus(false);
  }

  // Keep the QR chip's age live so a stale code visibly expires rather than
  // silently being dropped from the next submission with no explanation.
  useEffect(() => {
    if (!qr) return;
    const id = window.setInterval(() => setQrAgeMs(Date.now() - qr.capturedAt), 500);
    return () => window.clearInterval(id);
  }, [qr]);

  const qrIsFresh = qr !== null && qrAgeMs < QR_FRESHNESS_MS;

  const submit = useCallback(
    async (kind: "check_in" | "check_out") => {
      if (!campusId) return;
      setBusy(kind);
      setOutcome(null);

      const signals: Signals = {};
      if (geo) signals.geofence = geo;
      if (qrIsFresh && qr) signals.qr = { token: qr.token };

      const result = await submitCheck(kind, { campus_id: campusId, signals });
      setOutcome(result);
      setBusy(null);
      setQr(null);
      if (result.kind === "queued") flushNow();
    },
    [campusId, geo, qr, qrIsFresh, flushNow],
  );

  return (
    <div className="space-y-6 py-2">
      <header>
        <p className="eyebrow">eduCore</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          Check in
        </h1>
      </header>

      {campus && (
        <div className="flex items-center justify-between rounded-md border border-rule bg-card px-3.5 py-2.5 text-sm">
          <span>
            Checking in at <span className="font-medium">{campus.name}</span>
          </span>
          {campuses.length > 1 && (
            <button
              onClick={() => setPickingCampus((v) => !v)}
              className="text-xs font-medium text-signal underline"
            >
              Change
            </button>
          )}
        </div>
      )}

      {pickingCampus && (
        <ul className="space-y-1.5 rounded-lg border border-rule bg-card p-2">
          {campuses.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => chooseCampus(c.id)}
                className={`block w-full rounded-md px-3 py-2 text-left text-sm transition-colors ${
                  c.id === campusId
                    ? "bg-signal-soft font-medium text-signal"
                    : "text-ink hover:bg-paper"
                }`}
              >
                {c.name}
              </button>
            </li>
          ))}
        </ul>
      )}

      {pending > 0 && (
        <div className="flex items-center justify-between rounded-md border border-signal/70 bg-signal-soft px-3.5 py-2.5 text-xs text-signal">
          <span>
            <span className="measured">{pending}</span>{" "}
            {pending === 1 ? "event" : "events"} waiting to sync
            {syncing ? "…" : "."}
          </span>
          {!syncing && (
            <button onClick={flushNow} className="font-medium underline">
              Retry now
            </button>
          )}
        </div>
      )}

      {campusError && (
        <p role="alert" className="rounded-md bg-rejected-soft px-3.5 py-2.5 text-sm text-rejected">
          {campusError}
        </p>
      )}

      <section className="space-y-3 rounded-lg border border-rule bg-card p-4">
        <div className="grid grid-cols-2 gap-3">
          <Button
            onClick={() => submit("check_in")}
            disabled={!campusId || busy !== null}
            className="py-4 text-base"
          >
            {busy === "check_in" ? "Checking in…" : "Check in"}
          </Button>
          <Button
            variant="quiet"
            onClick={() => submit("check_out")}
            disabled={!campusId || busy !== null}
            className="py-4 text-base"
          >
            {busy === "check_out" ? "Checking out…" : "Check out"}
          </Button>
        </div>

        <dl className="grid grid-cols-2 gap-3 text-xs text-slate">
          <div>
            <dt className="eyebrow">Location</dt>
            <dd className="mt-1">
              {geo
                ? `Captured (±${geo.accuracy_m}m)`
                : "Unavailable — will still submit"}
            </dd>
          </div>
          <div>
            <dt className="eyebrow">Display code</dt>
            <dd className="mt-1">
              {qrIsFresh
                ? `Scanned ${Math.max(0, Math.round((QR_FRESHNESS_MS - qrAgeMs) / 1000))}s left`
                : "Not scanned"}
            </dd>
          </div>
        </dl>
      </section>

      <section className="rounded-lg border border-rule bg-card p-4">
        <p className="text-sm font-semibold">Staff-room display code</p>
        <p className="mt-1 text-xs text-slate">
          Scanning the code on the staff-room screen is the strongest evidence
          for your check-in. It&apos;s optional — you can still check in
          without it.
        </p>
        <div className="mt-3">
          <QrCapture
            onCapture={(token) => setQr({ token, capturedAt: Date.now() })}
          />
        </div>
      </section>

      {outcome && <StatusCard outcome={outcome} />}

      <Link
        href="/checkin/history"
        className="block rounded-md border border-rule bg-card px-4 py-3 text-center text-sm font-medium text-signal transition-colors hover:border-signal"
      >
        My attendance history →
      </Link>
    </div>
  );
}
