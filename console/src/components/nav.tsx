"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

const LINKS = [
  { href: "/", label: "Today" },
  { href: "/review", label: "Review" },
  { href: "/results", label: "Results" },
  { href: "/coverage", label: "Coverage" },
  { href: "/hod", label: "My department" },
  { href: "/at-risk", label: "At risk" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();

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

      <ul className="mt-8 flex-1 space-y-0.5">
        {LINKS.map((link) => {
          const active =
            link.href === "/"
              ? pathname === "/"
              : pathname.startsWith(link.href);
          return (
            <li key={link.href}>
              <Link
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`block rounded-md px-2 py-1.5 text-sm transition-colors ${
                  active
                    ? "bg-signal-soft font-medium text-signal"
                    : "text-slate hover:text-ink"
                }`}
              >
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>

      <button
        onClick={signOut}
        className="px-2 py-1.5 text-left text-sm text-slate transition-colors hover:text-ink"
      >
        Sign out
      </button>
    </nav>
  );
}
