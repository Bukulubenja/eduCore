/**
 * The evidence bar.
 *
 * Most dashboards project confidence they have not earned. This one shows its
 * working: every attendance record carries the signals behind it, each segment
 * sized by the weight it contributed and coloured by what it found.
 *
 * A deputy deciding an appeal needs to see *why* a check-in scored 62 -- that
 * the QR passed and the Wi-Fi was simply absent, not failed. Without that,
 * "provisional" is an accusation with no visible basis, and the review queue
 * becomes a rubber stamp.
 */

export type Signal = {
  signal_type: string;
  verdict: "pass" | "fail" | "unavailable";
  weight_applied: number;
  hard_fail?: boolean;
  detail?: string;
};

const LABELS: Record<string, string> = {
  qr: "Code",
  device: "Device",
  geofence: "Location",
  wifi: "Wi-Fi",
  time_window: "On time",
  beacon: "Beacon",
  face: "Face",
};

const VERDICT_STYLE: Record<Signal["verdict"], string> = {
  pass: "bg-verified",
  fail: "bg-rejected",
  // Not a failure. Missing evidence is drawn as absence, never as guilt.
  unavailable: "bg-rule",
};

export function EvidenceBar({ signals }: { signals: Signal[] }) {
  const weighted = signals.filter((s) => s.weight_applied > 0);
  if (weighted.length === 0) {
    return <p className="text-sm text-mist">No signals were recorded.</p>;
  }

  const total = weighted.reduce((sum, s) => sum + s.weight_applied, 0);

  return (
    <div>
      <div
        className="flex h-2 w-full gap-px overflow-hidden rounded-full bg-paper"
        role="img"
        aria-label={weighted
          .map((s) => `${LABELS[s.signal_type] ?? s.signal_type}: ${s.verdict}`)
          .join(", ")}
      >
        {weighted.map((signal) => (
          <span
            key={signal.signal_type}
            className={VERDICT_STYLE[signal.verdict]}
            style={{ width: `${(signal.weight_applied / total) * 100}%` }}
          />
        ))}
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
        {weighted.map((signal) => (
          <div key={signal.signal_type} className="flex items-baseline gap-1.5">
            <span
              aria-hidden
              className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                VERDICT_STYLE[signal.verdict]
              }`}
            />
            <dt className="text-xs text-slate">
              {LABELS[signal.signal_type] ?? signal.signal_type}
            </dt>
            <dd
              className={`measured text-xs ${
                signal.verdict === "pass"
                  ? "text-verified"
                  : signal.verdict === "fail"
                    ? "text-rejected"
                    : "text-mist"
              }`}
            >
              {signal.verdict === "unavailable" ? "n/a" : signal.verdict}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const DISPOSITION_STYLE: Record<string, string> = {
  verified: "bg-verified-soft text-verified",
  provisional: "bg-provisional-soft text-provisional",
  rejected: "bg-rejected-soft text-rejected",
};

export function Disposition({
  value,
  confidence,
}: {
  value: string;
  confidence?: number;
}) {
  return (
    <span
      className={`inline-flex items-baseline gap-2 rounded-full px-2.5 py-1 ${
        DISPOSITION_STYLE[value] ?? "bg-paper text-slate"
      }`}
    >
      <span className="text-xs font-medium capitalize">{value}</span>
      {confidence !== undefined && (
        <span className="measured text-xs opacity-70">{confidence}</span>
      )}
    </span>
  );
}
