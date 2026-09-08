"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Alert, Button, Empty, Panel } from "@/components/ui";

type PassOut = {
  id: string;
  student: string;
  reason: string;
  destination: string;
  responsible_person: string;
  status: string;
  expected_return_at: string;
  departed_at: string | null;
  is_overdue: boolean;
};

type Trip = {
  id: string;
  title: string;
  destination: string;
  departs_at: string;
  returns_at: string;
  status: string;
  participant_count: number;
  supervisor_count: number;
};

type OffCampus = {
  student_id: string;
  student_name: string;
  via: "pass_out" | "trip";
  expected_back: string;
  overdue: boolean;
};

const REASONS: Record<string, string> = {
  medical: "Medical",
  family: "Family",
  weekend_home: "Weekend at home",
  official: "Official school business",
  other: "Other",
};

function when(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function MovementPage() {
  const [passOuts, setPassOuts] = useState<PassOut[] | null>(null);
  const [trips, setTrips] = useState<Trip[] | null>(null);
  const [offCampus, setOffCampus] = useState<OffCampus[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([
      api.get<PassOut[]>("/pass-outs?open=true"),
      api.get<Trip[]>("/trips?status=submitted"),
      api.get<{ results: OffCampus[] }>("/movement/off-campus"),
    ])
      .then(([p, t, o]) => {
        setPassOuts(p);
        setTrips(t);
        setOffCampus(o.results);
      })
      .catch((err) => setError((err as Error).message));
  }, []);

  useEffect(load, [load]);

  async function act(key: string, call: () => Promise<unknown>) {
    setBusy(key);
    setError("");
    try {
      await call();
      load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (error && !passOuts) {
    return <Alert tone="rejected">{error}</Alert>;
  }
  if (!passOuts || !trips || !offCampus) {
    return <p className="text-caption text-mist">Loading movement…</p>;
  }

  const awaitingDecision = passOuts.filter((p) => p.status === "requested");
  const active = passOuts.filter(
    (p) => p.status === "approved" || p.status === "departed",
  );

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Movement</p>
        <h1 className="mt-2 text-h1">Student movement</h1>
        <p className="mt-3 max-w-xl text-body text-slate">
          Who is off campus, why, who approved it, and when they are due back.
        </p>
      </header>

      {error && <Alert tone="rejected">{error}</Alert>}

      <Panel title="Currently off campus" hint="Pass-outs and trips in progress">
        {offCampus.length === 0 ? (
          <Empty tone="success">Every student is on campus.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {offCampus.map((row) => (
              <li
                key={row.student_id}
                className="flex flex-wrap items-baseline justify-between gap-3 py-3 first:pt-0 last:pb-0"
              >
                <div>
                  <p className="text-body font-medium">{row.student_name}</p>
                  <p className="text-caption text-slate">
                    {row.via === "trip" ? "On a trip" : "Pass-out"} · due back{" "}
                    {when(row.expected_back)}
                  </p>
                </div>
                {row.overdue && (
                  <span className="text-caption font-medium text-rejected">
                    Overdue
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Pass-outs awaiting a decision">
        {awaitingDecision.length === 0 ? (
          <Empty>No pass-out requests are waiting.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {awaitingDecision.map((p) => (
              <li key={p.id} className="py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <p className="text-body font-medium">
                    {REASONS[p.reason] ?? p.reason} — {p.destination}
                  </p>
                  <p className="measured text-caption text-mist">
                    back {when(p.expected_return_at)}
                  </p>
                </div>
                <p className="mt-1 text-caption text-slate">
                  Collected by {p.responsible_person}
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    size="sm"
                    loading={busy === `po-approve-${p.id}`}
                    onClick={() =>
                      act(`po-approve-${p.id}`, () =>
                        api.post(`/pass-outs/${p.id}/decision`, {
                          approved: true,
                        }),
                      )
                    }
                  >
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="quiet"
                    loading={busy === `po-deny-${p.id}`}
                    onClick={() =>
                      act(`po-deny-${p.id}`, () =>
                        api.post(`/pass-outs/${p.id}/decision`, {
                          approved: false,
                        }),
                      )
                    }
                  >
                    Deny
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Approved pass-outs" hint="Mark departure and return">
        {active.length === 0 ? (
          <Empty>No pass-outs are in progress.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {active.map((p) => (
              <li
                key={p.id}
                className="flex flex-wrap items-center justify-between gap-3 py-4 first:pt-0 last:pb-0"
              >
                <div>
                  <p className="text-body font-medium">
                    {REASONS[p.reason] ?? p.reason} — {p.destination}
                    {p.is_overdue && (
                      <span className="ml-2 text-caption font-medium text-rejected">
                        Overdue
                      </span>
                    )}
                  </p>
                  <p className="text-caption text-slate">
                    {p.status === "departed"
                      ? `Left ${when(p.departed_at)}`
                      : "Not yet departed"}{" "}
                    · due back {when(p.expected_return_at)}
                  </p>
                </div>
                {p.status === "approved" ? (
                  <Button
                    size="sm"
                    loading={busy === `po-depart-${p.id}`}
                    onClick={() =>
                      act(`po-depart-${p.id}`, () =>
                        api.post(`/pass-outs/${p.id}/departure`, {}),
                      )
                    }
                  >
                    Mark departed
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="quiet"
                    loading={busy === `po-return-${p.id}`}
                    onClick={() =>
                      act(`po-return-${p.id}`, () =>
                        api.post(`/pass-outs/${p.id}/return`, {}),
                      )
                    }
                  >
                    Mark returned
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Trips awaiting approval">
        {trips.length === 0 ? (
          <Empty>No trips are waiting for approval.</Empty>
        ) : (
          <ul className="divide-y divide-rule">
            {trips.map((t) => (
              <li key={t.id} className="py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <p className="text-body font-medium">{t.title}</p>
                  <p className="measured text-caption text-mist">
                    {when(t.departs_at)} → {when(t.returns_at)}
                  </p>
                </div>
                <p className="mt-1 text-caption text-slate">
                  {t.destination} · {t.participant_count} student(s),{" "}
                  {t.supervisor_count} supervisor(s)
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    size="sm"
                    loading={busy === `trip-approve-${t.id}`}
                    onClick={() =>
                      act(`trip-approve-${t.id}`, () =>
                        api.post(`/trips/${t.id}/decision`, { approved: true }),
                      )
                    }
                  >
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="quiet"
                    loading={busy === `trip-reject-${t.id}`}
                    onClick={() =>
                      act(`trip-reject-${t.id}`, () =>
                        api.post(`/trips/${t.id}/decision`, { approved: false }),
                      )
                    }
                  >
                    Reject
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
