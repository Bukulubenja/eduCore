"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { AttendanceRecord } from "@/lib/checkin/types";

/**
 * A staff member's own attendance, with the disposition that produced it.
 *
 * "Symmetric visibility is a design requirement, not a courtesy" per
 * `MyAttendanceView`'s docstring in `educore/presence/views.py` -- this page
 * is that requirement's UI. `GET /me/attendance` already scopes to the caller
 * (via the request's membership), so no id or filter is needed here.
 */

const STATUS_LABEL: Record<AttendanceRecord["status"], string> = {
  present: "Present",
  late: "Late",
  partial: "Signed in, no sign-out",
  absent: "Absent",
  on_leave: "On leave",
  holiday: "Holiday",
};

const DISPOSITION_TONE: Record<AttendanceRecord["disposition"], string> = {
  verified: "text-verified",
  provisional: "text-provisional",
  rejected: "text-rejected",
};

function formatDate(iso: string) {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

function formatTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function AttendanceHistoryPage() {
  const [records, setRecords] = useState<AttendanceRecord[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      // `/me/attendance` is a paginated list endpoint -- it returns
      // `{results, next_cursor, has_more}`, not a bare array. This first
      // page (default ordering: most recent first) is enough for this
      // screen; add cursor paging here if history needs to go back further.
      .get<{ results: AttendanceRecord[] }>("/me/attendance")
      .then((body) => setRecords(body.results))
      .catch((err) => setError(err.message ?? "Couldn't load your history."));
  }, []);

  return (
    <div className="space-y-6 py-2">
      <header>
        <Link href="/checkin" className="text-xs text-slate hover:text-ink">
          ← Check in
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          My attendance
        </h1>
      </header>

      {error && (
        <p role="alert" className="rounded-md bg-rejected-soft px-3.5 py-2.5 text-sm text-rejected">
          {error}. This page needs a connection — your check-ins themselves
          still work offline.
        </p>
      )}

      {!records && !error && (
        <p className="text-sm text-mist">Loading…</p>
      )}

      {records && records.length === 0 && (
        <p className="text-sm text-mist">No attendance recorded yet.</p>
      )}

      {records && records.length > 0 && (
        <ul className="divide-y divide-rule rounded-lg border border-rule bg-card">
          {records.map((record) => (
            <li key={record.id} className="px-4 py-3.5">
              <div className="flex items-baseline justify-between gap-3">
                <p className="text-sm font-medium">{formatDate(record.date)}</p>
                <p className={`measured text-xs ${DISPOSITION_TONE[record.disposition]}`}>
                  {record.confidence}%
                </p>
              </div>
              <div className="mt-1 flex items-baseline justify-between gap-3 text-xs text-slate">
                <span>
                  {STATUS_LABEL[record.status]} · in {formatTime(record.first_in_at)} ·
                  {" "}out {formatTime(record.last_out_at)}
                </span>
              </div>
              {record.needs_review && (
                <p className="mt-1.5 text-xs text-provisional">
                  Flagged for review — a supervisor will confirm this.
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
