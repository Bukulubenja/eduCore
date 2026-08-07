"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Empty, Figure, Panel } from "@/components/ui";

type Today = {
  date: string;
  staff: {
    expected: number;
    checked_in: number;
    late: number;
    absent: number;
    not_checked_out: number;
    needs_review: number;
  };
  lessons: {
    scheduled: number;
    delivered: number;
    in_progress: number;
    missed: number;
    excused: number;
  };
  students: { marked: number; absent: number };
};

function longDate(iso: string) {
  const date = new Date(`${iso}T00:00:00`);
  return date.toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
}

/**
 * The day drawn as a proportion rather than a chart.
 *
 * A school day is a fixed number of periods, and the only question worth
 * answering at a glance is how much of it happened. A bar of segments says
 * that faster than five numbers, and it degrades honestly: before first
 * period, it is simply empty.
 */
function DayBar({ lessons }: { lessons: Today["lessons"] }) {
  const { scheduled, delivered, in_progress, missed, excused } = lessons;
  if (scheduled === 0) {
    return <Empty>No lessons are timetabled today.</Empty>;
  }

  const pending = Math.max(
    0,
    scheduled - delivered - in_progress - missed - excused,
  );
  const segments = [
    { key: "delivered", count: delivered, className: "bg-verified" },
    { key: "in_progress", count: in_progress, className: "bg-signal" },
    { key: "missed", count: missed, className: "bg-rejected" },
    { key: "excused", count: excused, className: "bg-provisional" },
    { key: "pending", count: pending, className: "bg-rule" },
  ].filter((segment) => segment.count > 0);

  return (
    <div className="settle">
      <div className="flex h-2.5 gap-px overflow-hidden rounded-full bg-paper">
        {segments.map((segment) => (
          <span
            key={segment.key}
            className={segment.className}
            style={{ width: `${(segment.count / scheduled) * 100}%` }}
          />
        ))}
      </div>

      <div className="mt-5 grid grid-cols-2 gap-5 sm:grid-cols-4">
        <Figure value={delivered} label="delivered" tone="verified" />
        <Figure value={in_progress} label="in progress" />
        <Figure
          value={missed}
          label="missed"
          tone={missed > 0 ? "rejected" : "mist"}
        />
        <Figure value={pending} label="still to come" tone="mist" />
      </div>
    </div>
  );
}

export default function TodayPage() {
  const [data, setData] = useState<Today | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<Today>("/insights/today")
      .then(setData)
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

  if (!data) {
    return <p className="text-sm text-mist">Loading today…</p>;
  }

  const { staff, lessons, students } = data;

  // What actually needs a person. Ordered by how quickly it goes stale: a
  // review left until tomorrow is a teacher waiting on a decision about their
  // own pay record.
  const actions = [
    staff.needs_review > 0 && {
      href: "/review",
      count: staff.needs_review,
      text: "check-ins need review",
    },
    lessons.missed > 0 && {
      href: "/coverage",
      count: lessons.missed,
      text: "lessons were missed and are unexplained",
    },
    staff.not_checked_out > 0 && {
      href: "/review",
      count: staff.not_checked_out,
      text: "staff signed in but never signed out",
    },
  ].filter(Boolean) as { href: string; count: number; text: string }[];

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Today</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          {longDate(data.date)}
        </h1>
      </header>

      <Panel
        title="Staff"
        hint={`${staff.checked_in} of ${staff.expected} expected have checked in`}
      >
        <div className="grid grid-cols-2 gap-5 sm:grid-cols-4">
          <Figure value={staff.checked_in} label="checked in" tone="verified" />
          <Figure
            value={staff.late}
            label="late"
            tone={staff.late > 0 ? "provisional" : "mist"}
          />
          <Figure
            value={staff.absent}
            label="not yet in"
            tone={staff.absent > 0 ? "rejected" : "mist"}
          />
          <Figure
            value={staff.not_checked_out}
            label="no sign-out"
            tone={staff.not_checked_out > 0 ? "provisional" : "mist"}
          />
        </div>
      </Panel>

      <Panel title="Lessons" hint="Against the timetable published for today">
        <DayBar lessons={lessons} />
      </Panel>

      <Panel title="Students" hint="From registers marked so far">
        <div className="grid grid-cols-2 gap-5 sm:grid-cols-4">
          <Figure value={students.marked} label="marked present or absent" />
          <Figure
            value={students.absent}
            label="absent"
            tone={students.absent > 0 ? "provisional" : "mist"}
          />
        </div>
      </Panel>

      <Panel title="Needs you">
        {actions.length === 0 ? (
          <Empty>
            Nothing is waiting. Every check-in today had clear evidence and no
            lesson has been missed.
          </Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {actions.map((action) => (
              <li key={action.text}>
                <Link
                  href={action.href}
                  className="flex items-baseline gap-3 py-3 text-sm transition-colors hover:text-signal"
                >
                  <span className="measured text-lg leading-none">
                    {action.count}
                  </span>
                  <span>{action.text}</span>
                  <span aria-hidden className="ml-auto text-mist">
                    →
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
