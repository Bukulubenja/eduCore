"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Empty, Panel } from "@/components/ui";

type Report = {
  scheme_id: string;
  class_group_id: string | null;
  coverage: number;
  expected: number;
  pace: number;
  is_behind: boolean;
  periods_elapsed: number;
  total_periods: number;
  units_completed: number;
  units_total: number;
};

const percent = (value: number) => `${Math.round(value * 100)}%`;

/**
 * Coverage against expectation, on one track.
 *
 * Coverage alone says nothing: 35% is fine in week 2 and a crisis in week 9.
 * So the bar shows what has been taught, and a marker shows where the class
 * should be by now. The gap between them is the only thing worth reading.
 */
function PaceTrack({ report }: { report: Report }) {
  return (
    <div className="relative h-2.5 rounded-full bg-paper">
      <div
        className={`h-full rounded-full ${
          report.is_behind ? "bg-provisional" : "bg-verified"
        }`}
        style={{ width: `${Math.min(100, report.coverage * 100)}%` }}
      />
      <span
        aria-hidden
        className="absolute top-1/2 h-4 w-0.5 -translate-y-1/2 bg-ink"
        style={{ left: `${Math.min(100, report.expected * 100)}%` }}
        title="Where this class should be by now"
      />
    </div>
  );
}

export default function CoveragePage() {
  const [reports, setReports] = useState<Report[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<{ results: Report[] }>("/coverage")
      .then((body) => setReports(body.results))
      .catch((err) => setError(err.message));
  }, []);

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

  if (!reports) {
    return <p className="text-sm text-mist">Loading coverage…</p>;
  }

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Coverage</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Classes falling behind
        </h1>
        <p className="mt-3 max-w-xl text-sm text-slate">
          Measured in lesson periods that actually happened, so a fortnight of
          examinations does not push every class into the red.
        </p>
      </header>

      <Panel
        title="Behind the plan"
        hint="Worst first. The marker shows where each class should be by now"
      >
        {reports.length === 0 ? (
          <Empty>
            No class is behind its scheme of work. Nothing to act on here.
          </Empty>
        ) : (
          <ul className="space-y-6">
            {reports.map((report) => (
              <li key={`${report.scheme_id}-${report.class_group_id}`}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="text-sm font-medium">
                    {report.units_completed} of {report.units_total} topics
                    finished
                  </p>
                  <p className="measured text-sm text-provisional">
                    {percent(report.pace)} behind
                  </p>
                </div>

                <div className="mt-2.5">
                  <PaceTrack report={report} />
                </div>

                <p className="mt-2 text-xs text-slate">
                  <span className="measured">{percent(report.coverage)}</span>{" "}
                  covered against{" "}
                  <span className="measured">{percent(report.expected)}</span>{" "}
                  expected after{" "}
                  <span className="measured">{report.periods_elapsed}</span> of{" "}
                  <span className="measured">{report.total_periods}</span>{" "}
                  planned periods
                </p>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
