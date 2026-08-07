"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Button } from "@/components/ui";

type Membership = { id: string; school: string; school_id: string };

function SignIn() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") ?? "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [choices, setChoices] = useState<Membership[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(membershipId?: string) {
    setBusy(true);
    setError("");

    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        membership_id: membershipId,
      }),
    });
    const body = await response.json().catch(() => ({}));
    setBusy(false);

    if (!response.ok) {
      setError(body.detail ?? "Email or password is incorrect.");
      return;
    }
    if (body.choose_membership) {
      setChoices(body.memberships);
      return;
    }

    router.push(next);
    router.refresh();
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-6 py-16">
      <div className="w-full max-w-sm">
        <p className="eyebrow">eduCore</p>
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">
          Sign in to your school
        </h1>

        {choices ? (
          <>
            <p className="mt-2 text-sm text-slate">
              You work at more than one school. Choose which one you are
              signing in to.
            </p>
            <ul className="mt-6 space-y-2">
              {choices.map((choice) => (
                <li key={choice.id}>
                  <button
                    onClick={() => submit(choice.id)}
                    disabled={busy}
                    className="w-full rounded-md border border-rule bg-card px-4 py-3 text-left text-sm font-medium transition-colors hover:border-signal disabled:opacity-50"
                  >
                    {choice.school}
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <form
            className="mt-6 space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <div>
              <label htmlFor="email" className="text-xs font-medium text-slate">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="mt-1.5 w-full rounded-md border border-rule bg-card px-3 py-2 text-sm outline-none focus:border-signal"
              />
            </div>

            <div>
              <label
                htmlFor="password"
                className="text-xs font-medium text-slate"
              >
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="mt-1.5 w-full rounded-md border border-rule bg-card px-3 py-2 text-sm outline-none focus:border-signal"
              />
            </div>

            {error && (
              <p
                role="alert"
                className="rounded-md bg-rejected-soft px-3 py-2 text-sm text-rejected"
              >
                {error}
              </p>
            )}

            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        )}
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <SignIn />
    </Suspense>
  );
}
