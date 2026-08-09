"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Button, Panel } from "@/components/ui";

const EDIT_ROLES = new Set(["director", "head_teacher", "dos"]);

/** Presentation only -- `POST /attendance/policy` is the actual gate
 * (`POLICY_ROLES` in `educore/presence/views.py`). This just decides whether
 * to render an editable form or a read-only one, so nobody outside those
 * roles fills in eleven fields before finding out at the very end they were
 * never allowed to save. */
function canEditPolicy(): boolean {
  if (typeof document === "undefined") return false;
  const match = document.cookie.match(/(?:^|; )ec_roles=([^;]*)/);
  if (!match) return false;
  try {
    const roles: string[] = JSON.parse(decodeURIComponent(match[1]));
    return roles.some((r) => EDIT_ROLES.has(r));
  } catch {
    return false;
  }
}

type Policy = {
  id: string;
  version: number;
  is_active: boolean;
  weights: Record<string, number>;
  accept_threshold: number;
  review_threshold: number;
  min_evidence_weight: number;
  geofence_radius_m: number;
  geofence_trusted_accuracy_m: number;
  day_starts_at: string;
  day_ends_at: string;
  late_grace_minutes: number;
  early_checkin_minutes: number;
  max_clock_skew_seconds: number;
  qr_token_ttl_seconds: number;
};

type FormState = {
  accept_threshold: number;
  review_threshold: number;
  min_evidence_weight: number;
  geofence_radius_m: number;
  geofence_trusted_accuracy_m: number;
  day_starts_at: string;
  day_ends_at: string;
  late_grace_minutes: number;
  early_checkin_minutes: number;
  max_clock_skew_seconds: number;
  qr_token_ttl_seconds: number;
};

function toForm(policy: Policy): FormState {
  return {
    accept_threshold: policy.accept_threshold,
    review_threshold: policy.review_threshold,
    min_evidence_weight: policy.min_evidence_weight,
    geofence_radius_m: policy.geofence_radius_m,
    geofence_trusted_accuracy_m: policy.geofence_trusted_accuracy_m,
    // A time input wants "HH:MM"; the API returns "HH:MM:SS".
    day_starts_at: policy.day_starts_at.slice(0, 5),
    day_ends_at: policy.day_ends_at.slice(0, 5),
    late_grace_minutes: policy.late_grace_minutes,
    early_checkin_minutes: policy.early_checkin_minutes,
    max_clock_skew_seconds: policy.max_clock_skew_seconds,
    qr_token_ttl_seconds: policy.qr_token_ttl_seconds,
  };
}

const FIELDS: {
  key: keyof FormState;
  label: string;
  hint: string;
  type: "number" | "time";
  min?: number;
  max?: number;
}[] = [
  {
    key: "accept_threshold",
    label: "Accept threshold",
    hint: "Score at or above this is verified, no review needed",
    type: "number",
    min: 0,
    max: 100,
  },
  {
    key: "review_threshold",
    label: "Review threshold",
    hint: "Below this, a check-in is rejected outright rather than flagged",
    type: "number",
    min: 0,
    max: 100,
  },
  {
    key: "min_evidence_weight",
    label: "Minimum evidence weight",
    hint: "Total signal weight required before a score is trusted",
    type: "number",
    min: 0,
    max: 100,
  },
  {
    key: "geofence_radius_m",
    label: "Geofence radius (metres)",
    hint: "Distance from a campus still counted as on-site",
    type: "number",
    min: 0,
  },
  {
    key: "geofence_trusted_accuracy_m",
    label: "Trusted GPS accuracy (metres)",
    hint: "Below this reported accuracy, a fix outside the fence counts as evidence of absence",
    type: "number",
    min: 0,
  },
  {
    key: "day_starts_at",
    label: "Duty day starts",
    hint: "Default start time; a member's own schedule overrides this",
    type: "time",
  },
  {
    key: "day_ends_at",
    label: "Duty day ends",
    hint: "Default end time",
    type: "time",
  },
  {
    key: "late_grace_minutes",
    label: "Late grace (minutes)",
    hint: "Minutes after the start time still counted as on time",
    type: "number",
    min: 0,
  },
  {
    key: "early_checkin_minutes",
    label: "Early check-in window (minutes)",
    hint: "How early a check-in may arrive before the start time",
    type: "number",
    min: 0,
  },
  {
    key: "max_clock_skew_seconds",
    label: "Max clock skew (seconds)",
    hint: "Device clock drift tolerated before extra scrutiny applies",
    type: "number",
    min: 0,
  },
  {
    key: "qr_token_ttl_seconds",
    label: "QR code lifetime (seconds)",
    hint: "How long a displayed code stays valid before it rotates",
    type: "number",
    min: 1,
    max: 300,
  },
];

/**
 * Policy configuration.
 *
 * Saving never edits the active policy in place — it supersedes it with a new
 * version, the same guarantee `revise_policy` makes server-side, so every
 * attendance record already written can still be explained by the policy that
 * produced it (see `AttendancePolicy` in the presence app).
 */
export default function PolicyPage() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  // Computed client-side, not in state derived at render -- the roles cookie
  // doesn't change within a page view, and this avoids a first-paint flash
  // of the editable form before a client effect could downgrade it.
  const canEdit = canEditPolicy();

  useEffect(() => {
    api
      .get<Policy>("/attendance/policy")
      .then((body) => {
        setPolicy(body);
        setForm(toForm(body));
      })
      .catch((err) => setLoadError(err.message));
  }, []);

  function update<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSavedVersion(null);
  }

  async function save() {
    if (!form) return;
    setSaving(true);
    setSaveError("");
    try {
      const updated = await api.post<Policy>("/attendance/policy", form);
      setPolicy(updated);
      setForm(toForm(updated));
      setSavedVersion(updated.version);
    } catch (err) {
      setSaveError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (loadError) {
    return (
      <p
        role="alert"
        className="rounded-md bg-rejected-soft px-4 py-3 text-sm text-rejected"
      >
        {loadError}
      </p>
    );
  }

  if (!policy || !form) {
    return <p className="text-sm text-mist">Loading policy…</p>;
  }

  return (
    <div className="space-y-8">
      <header>
        <p className="eyebrow">Policy</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">
          Attendance-confidence thresholds
        </h1>
        <p className="mt-3 max-w-xl text-sm text-slate">
          Saving does not edit these rules in place. It supersedes the active
          policy with a new version, so every existing attendance record can
          still be explained by the policy that produced it.
        </p>
      </header>

      {!canEdit && (
        <p className="rounded-md bg-signal-soft px-4 py-3 text-sm text-signal">
          You can see these thresholds, but your role can&rsquo;t change them.
          Ask a director, head teacher, or director of studies to make an
          edit.
        </p>
      )}

      <Panel
        title="Thresholds and duty window"
        hint={`Active since version ${policy.version}`}
        action={
          canEdit && (
            <Button onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save as new version"}
            </Button>
          )
        }
      >
        <div className="grid gap-5 sm:grid-cols-2">
          {FIELDS.map((field) => (
            <label key={field.key} className="block">
              <span className="text-sm font-medium">{field.label}</span>
              <input
                type={field.type}
                min={field.min}
                max={field.max}
                value={form[field.key]}
                disabled={!canEdit}
                onChange={(event) =>
                  update(
                    field.key,
                    (field.type === "number"
                      ? Number(event.target.value)
                      : event.target.value) as FormState[typeof field.key],
                  )
                }
                className="mt-1.5 w-full rounded-md border border-rule bg-card px-3 py-1.5 text-sm focus:border-signal focus:outline-none disabled:bg-paper disabled:text-slate"
              />
              <span className="mt-1 block text-xs text-slate">
                {field.hint}
              </span>
            </label>
          ))}
        </div>

        {savedVersion !== null && (
          <p className="mt-5 text-xs text-verified">
            Saved as version {savedVersion}.
          </p>
        )}
        {saveError && (
          <p role="alert" className="mt-5 text-xs text-rejected">
            {saveError}
          </p>
        )}
      </Panel>
    </div>
  );
}
