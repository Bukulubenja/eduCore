import type { SubmissionOutcome } from "@/lib/checkin/types";

/**
 * The three real outcomes this system produces, and only those three.
 *
 * "Accepted" covers both `verified` and `provisional` dispositions -- the
 * server never refuses a check-in for weak evidence (ADR-0002) -- but the two
 * are shown differently on purpose: provisional evidence is disclosed
 * plainly, not folded into a generic success message, because the person
 * whose attendance this is has the clearest reason to understand why their
 * evidence came back thin.
 */
export function StatusCard({ outcome }: { outcome: SubmissionOutcome }) {
  if (outcome.kind === "queued") {
    return (
      <div className="settle rounded-lg border border-signal/30 bg-signal-soft px-4 py-3.5">
        <p className="text-sm font-semibold text-signal">
          Queued — no connection
        </p>
        <p className="mt-1 text-xs text-slate">
          Saved on this device. It will be sent automatically as soon as
          you&apos;re back online — you don&apos;t need to do anything else.
        </p>
      </div>
    );
  }

  if (outcome.kind === "rejected") {
    return (
      <div className="settle rounded-lg border border-rejected/30 bg-rejected-soft px-4 py-3.5">
        <p className="text-sm font-semibold text-rejected">Could not submit</p>
        <p className="mt-1 text-xs text-slate">{outcome.reason}</p>
      </div>
    );
  }

  // outcome.kind === "accepted" from here — the server never refuses a
  // check-in for weak evidence (ADR-0002), so this branch's only job is to
  // present the disposition it actually settled on, not to guard against a
  // fourth kind.
  if (outcome.disposition === "verified") {
    return (
      <div className="settle rounded-lg border border-verified/30 bg-verified-soft px-4 py-3.5">
        <p className="text-sm font-semibold text-verified">Checked in</p>
        <p className="mt-1 text-xs text-slate">
          Evidence confirmed —{" "}
          <span className="measured">{outcome.confidence}%</span> confidence.
        </p>
      </div>
    );
  }

  if (outcome.disposition === "provisional") {
    return (
      <div className="settle rounded-lg border border-provisional/30 bg-provisional-soft px-4 py-3.5">
        <p className="text-sm font-semibold text-provisional">
          Recorded — flagged for review
        </p>
        <p className="mt-1 text-xs text-slate">
          The evidence was too thin to confirm automatically (
          <span className="measured">{outcome.confidence}%</span>{" "}
          confidence). This is not an error: it is on record and a supervisor
          will review it. Nothing more is needed from you right now.
        </p>
      </div>
    );
  }

  return (
    <div className="settle rounded-lg border border-rejected/30 bg-rejected-soft px-4 py-3.5">
      <p className="text-sm font-semibold text-rejected">Not accepted</p>
      <p className="mt-1 text-xs text-slate">
        The evidence contradicted your claim (
        <span className="measured">{outcome.confidence}%</span> confidence).
        If this is wrong, raise it from your attendance history.
      </p>
    </div>
  );
}
