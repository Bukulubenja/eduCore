"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Empty, Panel } from "@/components/ui";

type Reason = { code: "attendance" | "attainment"; detail: string };

type Student = {
  student_id: string;
  name: string;
  attendance_rate: number | null;
  marks_counted: number;
  average_percentage: number | null;
  assessments_counted: number;
  reasons: Reason[];
};

const REASON_LABEL: Record<Reason["code"], string> = {
  attendance: "Attendance",
  attainment: "Attainment",
};

export default function AtRiskPage() {
  const [students, setStudents] = useState<Student[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<{ results: Student[] }>("/insights/at-risk-students")
      .then((body) => setStudents(body.results))
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

  if (!students) {
    return <p className="text-sm text-mist">Loading…</p>;
  }

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">At risk</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Students to talk to
        </h1>
        <p className="mt-3 max-w-xl text-sm text-slate">
          Attendance and attainment are shown separately, never combined into a
          score. A conversation needs to know which one is wrong.
        </p>
      </header>

      <Panel
        title="Flagged this term"
        hint="Lowest attendance first"
      >
        {students.length === 0 ? (
          <Empty>
            No student meets either threshold. Attendance is above 80% and no
            released average is below 40%.
          </Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {students.map((student) => (
              <li key={student.student_id} className="py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <p className="text-sm font-medium">{student.name}</p>
                  <div className="flex items-baseline gap-4 text-xs">
                    {student.attendance_rate !== null && (
                      <span className="text-slate">
                        <span className="measured text-sm text-ink">
                          {Math.round(student.attendance_rate * 100)}%
                        </span>{" "}
                        present of{" "}
                        <span className="measured">
                          {student.marks_counted}
                        </span>
                      </span>
                    )}
                    {student.average_percentage !== null && (
                      <span className="text-slate">
                        <span className="measured text-sm text-ink">
                          {student.average_percentage}%
                        </span>{" "}
                        average
                      </span>
                    )}
                  </div>
                </div>

                <ul className="mt-2 space-y-1">
                  {student.reasons.map((reason) => (
                    <li
                      key={reason.code}
                      className="flex items-baseline gap-2 text-xs text-slate"
                    >
                      <span className="rounded bg-provisional-soft px-1.5 py-0.5 text-provisional">
                        {REASON_LABEL[reason.code]}
                      </span>
                      {reason.detail}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
