"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Disposition, EvidenceBar, type Signal } from "@/components/evidence";
import { Button, Empty, Panel } from "@/components/ui";

type Appeal = {
  id: string;
  record: string;
  raised_by: string;
  reason_code: string;
  narrative: string;
  requested_status: string;
  decision: string;
  created_at: string;
};

type Record_ = {
  id: string;
  membership_id: string;
  date: string;
  status: string;
  disposition: string;
  confidence: number;
  first_in_at: string | null;
  last_out_at: string | null;
  needs_review: boolean;
};

type Queue = {
  results: Appeal[];
  provisional_records?: Record_[];
};

type EventDetail = {
  results: { id: string; confidence: number; signals: Signal[] }[];
};

const REASONS: Record<string, string> = {
  device_fault: "Device fault",
  no_connectivity: "No connectivity",
  off_site_duty: "Off-site school duty",
  approved_leave: "Approved leave",
  sick: "Sick",
  other: "Other",
};

function time(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function ReviewPage() {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .get<Queue>("/attendance/reviews")
      .then(setQueue)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(load, [load]);

  async function decide(appealId: string, decision: "upheld" | "declined") {
    setBusy(appealId);
    try {
      await api.post(`/attendance/reviews/${appealId}/decide`, { decision });
      load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return (
      <p
        role="alert"
        className="rounded-md bg-rejected-soft px-4 py-3 text-sm text-rejected"
      >
        {error}
      </p>
    );
  }

  if (!queue) {
    return <p className="text-sm text-mist">Loading the queue…</p>;
  }

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Review</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Attendance needing a person
        </h1>
        <p className="mt-3 max-w-xl text-sm text-slate">
          A provisional record is not an accusation. It means the evidence was
          thin — usually a sensor that was unavailable, not one that failed.
        </p>
      </header>

      <Panel
        title="Appeals"
        hint="Raised by staff about their own record"
      >
        {queue.results.length === 0 ? (
          <Empty>No appeals are open.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {queue.results.map((appeal) => (
              <li key={appeal.id} className="py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <p className="text-sm font-medium">
                    {REASONS[appeal.reason_code] ?? appeal.reason_code}
                  </p>
                  <p className="measured text-xs text-mist">
                    {new Date(appeal.created_at).toLocaleDateString("en-GB")}
                  </p>
                </div>

                {appeal.narrative && (
                  <blockquote className="mt-2 border-l-2 border-rule pl-3 text-sm text-slate">
                    {appeal.narrative}
                  </blockquote>
                )}

                <div className="mt-3 flex gap-2">
                  <Button
                    onClick={() => decide(appeal.id, "upheld")}
                    disabled={busy === appeal.id}
                  >
                    Uphold
                  </Button>
                  <Button
                    variant="quiet"
                    onClick={() => decide(appeal.id, "declined")}
                    disabled={busy === appeal.id}
                  >
                    Decline
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel
        title="Provisional records"
        hint="Recorded and counted. Flagged because the evidence was thin"
      >
        {!queue.provisional_records || queue.provisional_records.length === 0 ? (
          <Empty>
            Nothing is provisional. Every check-in scored above the school&rsquo;s
            accept threshold.
          </Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {queue.provisional_records.map((record) => (
              <ProvisionalRow key={record.id} record={record} />
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

/**
 * One provisional record, with the evidence behind it fetched on demand.
 *
 * Loaded lazily rather than with the list: a deputy opens two or three of
 * these, not forty, and pulling every signal set up front would make the queue
 * slow to show the thing it exists to show.
 */
function ProvisionalRow({ record }: { record: Record_ }) {
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [open, setOpen] = useState(false);

  async function reveal() {
    setOpen(true);
    if (signals) return;
    try {
      const body = await api.get<EventDetail>(
        `/me/attendance?from=${record.date}&to=${record.date}`,
      );
      setSignals(body.results?.[0]?.signals ?? []);
    } catch {
      setSignals([]);
    }
  }

  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <span className="measured text-sm">{record.date}</span>
          <span className="text-sm capitalize text-slate">{record.status}</span>
          <span className="measured text-xs text-mist">
            {time(record.first_in_at)} – {time(record.last_out_at)}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <Disposition
            value={record.disposition}
            confidence={record.confidence}
          />
          {!open && (
            <Button variant="quiet" onClick={reveal}>
              Show evidence
            </Button>
          )}
        </div>
      </div>

      {open && (
        <div className="mt-4">
          {signals === null ? (
            <p className="text-xs text-mist">Loading evidence…</p>
          ) : (
            <EvidenceBar signals={signals} />
          )}
        </div>
      )}
    </li>
  );
}
