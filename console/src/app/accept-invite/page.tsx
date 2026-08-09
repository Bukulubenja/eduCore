"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { Button } from "@/components/ui";

type Status = "checking" | "valid" | "invalid";

function AcceptInvite() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") ?? "";

  const [status, setStatus] = useState<Status>(token ? "checking" : "invalid");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    fetch(`/api/auth/accept-invite?token=${encodeURIComponent(token)}`)
      .then((response) => response.json())
      .then((body) => {
        if (!cancelled) setStatus(body.valid ? "valid" : "invalid");
      })
      .catch(() => {
        if (!cancelled) setStatus("invalid");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function submit() {
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }

    setBusy(true);
    const response = await fetch("/api/auth/accept-invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, password }),
    });
    const body = await response.json().catch(() => ({}));
    setBusy(false);

    if (!response.ok) {
      setError(body.detail ?? "This invitation link is invalid or has expired.");
      return;
    }

    router.push("/");
    router.refresh();
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-6 py-16">
      <div className="w-full max-w-sm">
        <p className="eyebrow">eduCore</p>
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">
          Set your password
        </h1>

        {status === "checking" && (
          <p className="mt-6 text-sm text-slate">Checking your invitation…</p>
        )}

        {status === "invalid" && (
          <p
            role="alert"
            className="mt-6 rounded-md bg-rejected-soft px-3 py-2 text-sm text-rejected"
          >
            This invitation link is invalid or has expired. Ask whoever set up
            your account to send a new one.
          </p>
        )}

        {status === "valid" && (
          <form
            className="mt-6 space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <div>
              <label
                htmlFor="password"
                className="text-xs font-medium text-slate"
              >
                New password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="mt-1.5 w-full rounded-md border border-rule bg-card px-3 py-2 text-sm outline-none focus:border-signal"
              />
            </div>

            <div>
              <label
                htmlFor="confirm"
                className="text-xs font-medium text-slate"
              >
                Confirm password
              </label>
              <input
                id="confirm"
                type="password"
                autoComplete="new-password"
                required
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
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
              {busy ? "Setting password…" : "Set password and sign in"}
            </Button>
          </form>
        )}
      </div>
    </main>
  );
}

export default function AcceptInvitePage() {
  return (
    <Suspense>
      <AcceptInvite />
    </Suspense>
  );
}
