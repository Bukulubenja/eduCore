import type { ReactNode } from "react";

export function Panel({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-rule bg-card">
      <header className="flex items-baseline justify-between gap-4 border-b border-rule px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {hint && <p className="mt-0.5 text-xs text-slate">{hint}</p>}
        </div>
        {action}
      </header>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

/**
 * A measured figure and what it counts.
 *
 * The number is mono and the label is not, which is the same rule used
 * everywhere else: the system counted the figure, a person wrote the label.
 */
export function Figure({
  value,
  label,
  tone = "ink",
}: {
  value: number | string;
  label: string;
  tone?: "ink" | "verified" | "provisional" | "rejected" | "mist";
}) {
  const toneClass = {
    ink: "text-ink",
    verified: "text-verified",
    provisional: "text-provisional",
    rejected: "text-rejected",
    mist: "text-mist",
  }[tone];

  return (
    <div>
      <p className={`measured text-2xl leading-none ${toneClass}`}>{value}</p>
      <p className="mt-1.5 text-xs text-slate">{label}</p>
    </div>
  );
}

/**
 * Empty states are an invitation, not a shrug. Each one says what is true and,
 * where there is something to do, what to do.
 */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-slate">{children}</p>;
}

export function Button({
  children,
  variant = "primary",
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "quiet";
}) {
  const styles =
    variant === "primary"
      ? "bg-signal text-white hover:bg-ink"
      : "border border-rule bg-card text-ink hover:border-mist";

  return (
    <button
      {...props}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 ${styles} ${className ?? ""}`}
    >
      {children}
    </button>
  );
}

export function Rule() {
  return <hr className="border-rule" />;
}
