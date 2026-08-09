"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const OPERATIONAL_LINKS = [
  { href: "/", label: "Today" },
  { href: "/review", label: "Review" },
  { href: "/results", label: "Results" },
  { href: "/coverage", label: "Coverage" },
  { href: "/hod", label: "My department" },
  { href: "/at-risk", label: "At risk" },
];

// Configuration, not day-to-day operation -- kept visually distinct from the
// links above rather than sorted alphabetically into them. Still visible to
// everyone: /policy is readable by anyone and only refuses a save from the
// wrong role, so hiding it entirely would just be security theatre.
const ADMIN_LINKS = [{ href: "/policy", label: "Policy" }];

function linkClass(active: boolean) {
  return `block rounded-md px-2 py-1.5 text-sm transition-colors ${
    active ? "bg-signal-soft font-medium text-signal" : "text-slate hover:text-ink"
  }`;
}

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();

  function isActive(href: string) {
    return href === "/" ? pathname === "/" : pathname.startsWith(href);
  }

  async function signOut() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  return (
    <nav className="flex w-48 shrink-0 flex-col border-r border-rule bg-card px-4 py-6">
      <Link href="/" className="px-2 text-sm font-semibold tracking-tight">
        eduCore
      </Link>

      <ul className="mt-8 space-y-0.5">
        {OPERATIONAL_LINKS.map((link) => (
          <li key={link.href}>
            <Link
              href={link.href}
              aria-current={isActive(link.href) ? "page" : undefined}
              className={linkClass(isActive(link.href))}
            >
              {link.label}
            </Link>
          </li>
        ))}
      </ul>

      <ul className="mt-4 space-y-0.5 border-t border-rule pt-4">
        {ADMIN_LINKS.map((link) => (
          <li key={link.href}>
            <Link
              href={link.href}
              aria-current={isActive(link.href) ? "page" : undefined}
              className={linkClass(isActive(link.href))}
            >
              {link.label}
            </Link>
          </li>
        ))}
      </ul>

      <div className="flex-1" />

      {/* Not part of this dashboard's route group -- its own mobile-first
          shell (`src/app/checkin/layout.tsx`) -- but otherwise unreachable
          except by direct URL, which left reviewers unable to see the
          surface they're adjudicating evidence from. */}
      <Link
        href="/checkin"
        className="px-2 py-1.5 text-xs text-mist transition-colors hover:text-slate"
      >
        Staff check-in ↗
      </Link>

      <button
        onClick={signOut}
        className="mt-2 px-2 py-1.5 text-left text-sm text-slate transition-colors hover:text-ink"
      >
        Sign out
      </button>
    </nav>
  );
}
