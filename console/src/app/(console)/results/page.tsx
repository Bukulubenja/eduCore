"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { StepUp } from "@/components/step-up";
import { Button, Empty, Panel } from "@/components/ui";

type Assessment = {
  id: string;
  title: string;
  kind: string;
  state: string;
  max_score: string;
  scheduled_for: string | null;
  returned_note: string;
  released_at: string | null;
};

type Anomaly = { code: string; detail: string };

type Scores = {
  assessment_id: string;
  state: string;
  max_score: string;
  scores: {
    student_id: string;
    full_name: string;
    raw_score: string | null;
    is_absent: boolean;
    percentage: number | null;
  }[];
  anomalies?: Anomaly[];
};

/**
 * The lifecycle, in the order it happens. Anything not in this list is not a
 * state a person acts on from this screen.
 */
const STAGES: { state: string; label: string; hint: string }[] = [
  {
    state: "head_review",
    label: "Awaiting release",
    hint: "Moderated. Nothing is visible to students or parents until released",
  },
  {
    state: "moderation",
    label: "In moderation",
    hint: "Marks submitted by the teacher, awaiting the director of studies",
  },
  {
    state: "submitted",
    label: "Papers awaiting approval",
    hint: "Submitted by the author, not yet approved to sit",
  },
];

const KINDS: Record<string, string> = {
  exercise: "Exercise",
  test: "Class test",
  midterm: "Mid-term",
  end_of_term: "End of term",
  mock: "Mock",
  national: "National",
};

const ANOMALY_LABELS: Record<string, string> = {
  identical_marks: "Every candidate scored the same",
  no_spread: "No spread between marks",
  implausibly_high: "Mean is close to the maximum",
};

export default function ResultsPage() {
  const [assessments, setAssessments] = useState<Assessment[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .get<{ results: Assessment[] }>("/assessments")
      .then((body) => setAssessments(body.results))
      .catch((err) => setError(err.message));
  }, []);

  useEffect(load, [load]);

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

  if (!assessments) {
    return <p className="text-sm text-mist">Loading assessments…</p>;
  }

  const released = assessments.filter((a) => a.state === "released");

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Results</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Marks awaiting a decision
        </h1>
        <p className="mt-3 max-w-xl text-sm text-slate">
          Releasing publishes marks to students and parents. It is recorded
          against you by name, and a correction afterwards is a visible revision
          rather than an edit.
        </p>
      </header>

      {STAGES.map((stage) => {
        const rows = assessments.filter((a) => a.state === stage.state);
        return (
          <Panel key={stage.state} title={stage.label} hint={stage.hint}>
            {rows.length === 0 ? (
              <Empty>Nothing here.</Empty>
            ) : (
              <ul className="divide-y divide-rule">
                {rows.map((assessment) => (
                  <AssessmentRow
                    key={assessment.id}
                    assessment={assessment}
                    onChanged={load}
                  />
                ))}
              </ul>
            )}
          </Panel>
        );
      })}

      <Panel title="Released" hint="Visible to students and parents">
        {released.length === 0 ? (
          <Empty>Nothing has been released this term.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {released.map((assessment) => (
              <li
                key={assessment.id}
                className="flex flex-wrap items-baseline justify-between gap-3 py-3 first:pt-0 last:pb-0"
              >
                <span className="text-sm">{assessment.title}</span>
                <span className="measured text-xs text-mist">
                  {assessment.released_at
                    ? new Date(assessment.released_at).toLocaleDateString(
                        "en-GB",
                      )
                    : "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

function AssessmentRow({
  assessment,
  onChanged,
}: {
  assessment: Assessment;
  onChanged: () => void;
}) {
  const [scores, setScores] = useState<Scores | null>(null);
  const [open, setOpen] = useState(false);
  const [steppingUp, setSteppingUp] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function reveal() {
    setOpen(true);
    if (scores) return;
    try {
      setScores(await api.get<Scores>(`/assessments/${assessment.id}/scores`));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function release(stepUpToken: string) {
    setBusy(true);
    try {
      await api.post(`/assessments/${assessment.id}/release`, {
        step_up_token: stepUpToken,
      });
      setSteppingUp(false);
      onChanged();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  async function transition(action: string) {
    setBusy(true);
    try {
      await api.post(`/assessments/${assessment.id}/${action}`, {});
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const anomalies = scores?.anomalies ?? [];
  const releasable = assessment.state === "head_review";

  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="text-sm font-medium">{assessment.title}</p>
          <p className="mt-0.5 text-xs text-slate">
            {KINDS[assessment.kind] ?? assessment.kind}
            <span className="measured"> · out of {assessment.max_score}</span>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!open && (
            <Button variant="quiet" onClick={reveal}>
              Show marks
            </Button>
          )}
          {assessment.state === "submitted" && (
            <Button onClick={() => transition("approve")} disabled={busy}>
              Approve
            </Button>
          )}
          {assessment.state === "moderation" && (
            <Button onClick={() => transition("moderate")} disabled={busy}>
              Moderate
            </Button>
          )}
          {releasable && !steppingUp && (
            <Button onClick={() => setSteppingUp(true)} disabled={busy}>
              Release…
            </Button>
          )}
        </div>
      </div>

      {assessment.returned_note && (
        <blockquote className="mt-2 border-l-2 border-rule pl-3 text-sm text-slate">
          {assessment.returned_note}
        </blockquote>
      )}

      {/*
        Anomalies sit above the release control, never behind a disclosure.
        A moderator who has to go looking for the warning is a moderator who
        will not see it on the afternoon it matters.
      */}
      {anomalies.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {anomalies.map((anomaly) => (
            <li
              key={anomaly.code}
              className="rounded-md bg-provisional-soft px-3 py-2"
            >
              <p className="text-xs font-medium text-provisional">
                {ANOMALY_LABELS[anomaly.code] ?? anomaly.code}
              </p>
              <p className="mt-0.5 text-xs text-slate">{anomaly.detail}</p>
            </li>
          ))}
        </ul>
      )}

      {open && scores && (
        <table className="mt-4 w-full text-sm">
          <thead>
            <tr className="border-b border-rule text-left">
              <th className="pb-1.5 font-medium text-slate">Student</th>
              <th className="pb-1.5 text-right font-medium text-slate">Mark</th>
              <th className="pb-1.5 text-right font-medium text-slate">%</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-rule">
            {scores.scores.map((score) => (
              <tr key={score.student_id}>
                <td className="py-1.5">{score.full_name}</td>
                <td className="measured py-1.5 text-right">
                  {score.is_absent ? (
                    <span className="text-mist">absent</span>
                  ) : (
                    (score.raw_score ?? "—")
                  )}
                </td>
                <td className="measured py-1.5 text-right text-slate">
                  {score.percentage === null ? "—" : score.percentage.toFixed(1)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {steppingUp && (
        <StepUp
          action={assessment.title}
          onGranted={release}
          onCancel={() => setSteppingUp(false)}
        />
      )}

      {error && (
        <p role="alert" className="mt-2 text-xs text-rejected">
          {error}
        </p>
      )}
    </li>
  );
}
